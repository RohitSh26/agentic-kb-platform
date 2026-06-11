"""The .copilot/ and .opencode/ renderings stay parity-pinned to agents/ (ADR-0009).

The canonical manifests in agents/ are the source of truth for tool access,
budgets, and instruction content. The host-native renderings are hand-authored,
so these contract tests are what keep them honest: tool lists exact-match the
canonical allowed_tools, budgets/evidence rules/output schemas appear in every
rendered body, host validity rules hold, and no shipped file ever carries a
token value (contract: docs/contracts/portable-agent-framework.md).
"""

import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
AGENTS_DIR = REPO_ROOT / "agents"
OPENCODE_DIR = REPO_ROOT / ".opencode"
COPILOT_DIR = REPO_ROOT / ".copilot"

ROLES = (
    "orchestrator",
    "implementation",
    "test_layer",
    "code_reviewer",
    "delivery_planner",
    "pr_planner",
)
SKILLS = ("evidence-pack-orchestration", "context-request-discipline", "evidence-citation")
OPENCODE_SKILL_NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
COPILOT_MAX_BODY_CHARS = 30_000
REQUEST_MORE_FIELDS = ("question", "why_needed", "decision_needed", "already_checked", "max_tokens")
SECRET_MARKERS = ("ghp_", "github_pat_", "secret")
# Reference-only credential shapes: $COPILOT_MCP_* / ${COPILOT_MCP_*} / ${input:...} / {env:...}
AUTH_REFERENCE_RE = re.compile(
    r"^Bearer ("
    r"\$COPILOT_MCP_[A-Z0-9_]+"
    r"|\$\{COPILOT_MCP_[A-Z0-9_]+\}"
    r"|\$\{input:[a-z0-9-]+\}"
    r"|\{env:[A-Z0-9_]+\}"
    r")$"
)

pytestmark = pytest.mark.skipif(
    not AGENTS_DIR.is_dir(), reason="agents/ manifests not present in this checkout"
)


def _split(path: Path) -> tuple[list[str], str]:
    lines = path.read_text().splitlines()
    assert lines and lines[0] == "---", f"{path}: must start with frontmatter"
    for i, line in enumerate(lines[1:], start=1):
        if line == "---":
            return lines[1:i], "\n".join(lines[i + 1 :])
    raise AssertionError(f"{path}: unterminated frontmatter")


def _parse_fields(lines: list[str], path: Path) -> dict[str, object]:
    """Hand-rolled frontmatter parsing (mcp-server has no pyyaml; do not add one).

    Handles the three shapes the canon and renderings use: ``key: value``,
    block lists (``  - item``), block maps (``  key: value``), and inline
    flow lists (``key: ['a', 'b']``).
    """
    fields: dict[str, object] = {}
    container: list[str] | dict[str, str] | None = None
    pending: str | None = None
    for line in lines:
        if line.startswith("  - "):
            if pending is not None:
                container = []
                fields[pending] = container
                pending = None
            assert isinstance(container, list), f"{path}: list item outside a list"
            container.append(line.removeprefix("  - ").strip())
        elif line.startswith("  "):
            if pending is not None:
                container = {}
                fields[pending] = container
                pending = None
            assert isinstance(container, dict), f"{path}: map entry outside a map"
            key, sep, raw = line.strip().partition(":")
            assert sep, f"{path}: bad nested line {line!r}"
            container[key.strip()] = raw.strip()
        else:
            container = None
            key, _, raw = line.partition(":")
            value = raw.strip()
            if value == "":
                pending = key.strip()
                fields[pending] = value
            elif value.startswith("[") and value.endswith("]"):
                pending = None
                fields[key.strip()] = [
                    item.strip().strip("'\"") for item in value[1:-1].split(",") if item.strip()
                ]
            else:
                pending = None
                fields[key.strip()] = value
    return fields


def _read(path: Path) -> tuple[dict[str, object], str]:
    frontmatter, body = _split(path)
    return _parse_fields(frontmatter, path), body


def _canon(role: str) -> tuple[dict[str, object], str]:
    return _read(AGENTS_DIR / f"{role}.md")


def _canon_tools(role: str) -> list[str]:
    tools = _canon(role)[0]["allowed_tools"]
    assert isinstance(tools, list) and tools
    return tools


def _opencode_agent(role: str) -> tuple[dict[str, object], str]:
    return _read(OPENCODE_DIR / "agents" / f"{role}.md")


def _copilot_agent(role: str) -> tuple[dict[str, object], str]:
    return _read(COPILOT_DIR / "agents" / f"{role}.agent.md")


def _rendered_bodies(role: str) -> dict[str, str]:
    return {"opencode": _opencode_agent(role)[1], "copilot": _copilot_agent(role)[1]}


def _shipped_files() -> list[Path]:
    files = [
        path
        for tree in (OPENCODE_DIR, COPILOT_DIR)
        for path in sorted(tree.rglob("*"))
        if path.is_file()
    ]
    assert files, "renderings missing"
    return files


# --- parity: every rendering preserves the canon ---------------------------------------------


def test_all_six_manifests_exist_in_both_renderings_plus_templates() -> None:
    for role in ROLES:
        assert (OPENCODE_DIR / "agents" / f"{role}.md").is_file(), f"opencode missing {role}"
        assert (COPILOT_DIR / "agents" / f"{role}.agent.md").is_file(), f"copilot missing {role}"
    assert (OPENCODE_DIR / "agents" / "_template.md").is_file()
    assert (COPILOT_DIR / "agents" / "_template.agent.md").is_file()


def test_opencode_frontmatter_tools_exactly_match_canonical_allowed_tools() -> None:
    for role in ROLES:
        expected = [f"context-broker_{tool}" for tool in _canon_tools(role)]
        tools = _opencode_agent(role)[0]["tools"]
        assert isinstance(tools, dict), f"{role}: opencode tools must be a map"
        assert list(tools) == expected, f"{role}: opencode tools drifted from canon"
        assert all(v == "true" for v in tools.values()), f"{role}: every entry must enable"


def test_opencode_json_per_agent_tools_exactly_match_canonical_allowed_tools() -> None:
    config = json.loads((OPENCODE_DIR / "opencode.json").read_text())
    assert config["tools"] == {"context-broker_*": False}, "broker namespace must default off"
    agents = config["agent"]
    assert set(agents) == set(ROLES)
    for role in ROLES:
        expected = {f"context-broker_{tool}": True for tool in _canon_tools(role)}
        assert agents[role]["tools"] == expected, f"{role}: opencode.json drifted from canon"


def test_copilot_frontmatter_tools_exactly_match_canonical_allowed_tools() -> None:
    for role in ROLES:
        expected = [f"context-broker/{tool}" for tool in _canon_tools(role)]
        fields, _ = _copilot_agent(role)
        assert fields["tools"] == expected, f"{role}: copilot tools drifted from canon"
        assert fields["name"] == _canon(role)[0]["name"], f"{role}: copilot name drifted"


def test_orchestrator_only_tools_never_leak_to_specialists() -> None:
    for role in ROLES:
        if role == "orchestrator":
            continue
        renderings = (("opencode", _opencode_agent(role)[0]), ("copilot", _copilot_agent(role)[0]))
        for host, fields in renderings:
            tools = fields["tools"]
            assert isinstance(tools, list | dict)
            rendered = list(tools)
            assert not any("create_pack" in t for t in rendered), f"{host}/{role}: pack leaked"
            assert not any("ledger." in t for t in rendered), f"{host}/{role}: ledger leaked"


def test_rendered_bodies_contain_the_canonical_instruction_body_verbatim() -> None:
    for role in ROLES:
        canonical_body = _canon(role)[1].strip()
        for host, body in _rendered_bodies(role).items():
            assert canonical_body in body, f"{host}/{role}: canonical body not verbatim"


def test_budget_numbers_in_every_rendered_body_match_the_canon() -> None:
    for role in ROLES:
        fields = _canon(role)[0]
        for host, body in _rendered_bodies(role).items():
            assert f"max_context_calls: {fields['max_context_calls']}" in body, f"{host}/{role}"
            assert f"max_context_tokens: {fields['max_context_tokens']}" in body, f"{host}/{role}"


def test_evidence_id_rule_in_every_rendered_body() -> None:
    for role in ROLES:
        for host, body in _rendered_bodies(role).items():
            lowered = body.lower()
            assert "evidence id" in lowered, f"{host}/{role}: evidence-ID rule missing"
            assert "open question" in lowered, f"{host}/{role}: open-question rule missing"


def test_request_more_field_discipline_in_every_rendered_body() -> None:
    for role in ROLES:
        for host, body in _rendered_bodies(role).items():
            for field in REQUEST_MORE_FIELDS:
                assert field in body, f"{host}/{role}: request_more field {field} missing"


def test_untrusted_content_rule_in_every_rendered_body() -> None:
    for role in ROLES:
        for host, body in _rendered_bodies(role).items():
            assert "untrusted" in body.lower(), f"{host}/{role}: untrusted-content rule missing"


def test_output_schema_named_in_every_rendered_body() -> None:
    for role in ROLES:
        schema = _canon(role)[0]["output_schema"]
        assert isinstance(schema, str)
        for host, body in _rendered_bodies(role).items():
            assert schema in body, f"{host}/{role}: output_schema {schema} not named"


def test_provenance_comment_in_every_rendered_body() -> None:
    for role in ROLES:
        for host, body in _rendered_bodies(role).items():
            assert f"<!-- rendered from agents/{role}.md" in body, (
                f"{host}/{role}: provenance comment missing"
            )


def test_copilot_repository_settings_allowlist_is_the_union_of_canonical_tools() -> None:
    config = json.loads((COPILOT_DIR / "mcp" / "repository-settings.json").read_text())
    allowed = config["mcpServers"]["context-broker"]["tools"]
    expected = sorted({tool for role in ROLES for tool in _canon_tools(role)})
    assert allowed != ["*"], "server-level allowlist must enumerate tools, not expose everything"
    assert sorted(allowed) == expected, "repository-settings allowlist drifted from canon union"


# --- validity: each rendering is well-formed for its host ------------------------------------


def test_opencode_modes_are_primary_for_orchestrator_and_subagent_for_the_rest() -> None:
    for role in ROLES:
        expected = "primary" if role == "orchestrator" else "subagent"
        assert _opencode_agent(role)[0]["mode"] == expected, f"{role}: wrong opencode mode"
    template, _ = _read(OPENCODE_DIR / "agents" / "_template.md")
    assert template["mode"] == "subagent"


def test_opencode_skills_follow_the_naming_rules() -> None:
    skills_dir = OPENCODE_DIR / "skills"
    assert {p.name for p in skills_dir.iterdir() if p.is_dir()} == set(SKILLS)
    for skill in SKILLS:
        fields, body = _read(skills_dir / skill / "SKILL.md")
        name = fields["name"]
        assert name == skill, f"{skill}: SKILL.md name must match its directory"
        assert isinstance(name, str) and OPENCODE_SKILL_NAME_RE.fullmatch(name), f"bad name {name}"
        assert len(name) <= 64
        description = fields["description"]
        assert isinstance(description, str) and description, f"{skill}: description required"
        assert len(description) <= 1024, f"{skill}: description over the documented limit"
        assert body.strip(), f"{skill}: empty skill body"


def test_copilot_skill_modules_exist_for_the_same_three_procedures() -> None:
    for skill in SKILLS:
        path = COPILOT_DIR / "skills" / f"{skill}.md"
        assert path.is_file() and path.read_text().strip(), f"copilot skill {skill} missing"


def test_copilot_bodies_stay_under_the_30k_char_limit() -> None:
    for path in sorted((COPILOT_DIR / "agents").glob("*.agent.md")):
        _, body = _read(path)
        assert len(body) < COPILOT_MAX_BODY_CHARS, f"{path.name}: body over Copilot's limit"


def test_description_is_present_in_every_agent_rendering() -> None:
    paths = [
        *sorted((OPENCODE_DIR / "agents").glob("*.md")),
        *sorted((COPILOT_DIR / "agents").glob("*.agent.md")),
    ]
    assert len(paths) == 2 * (len(ROLES) + 1)
    for path in paths:
        fields, _ = _read(path)
        description = fields.get("description")
        assert isinstance(description, str) and description, f"{path.name}: description required"


def test_templates_carry_the_explicit_description_slot() -> None:
    templates = (
        OPENCODE_DIR / "agents" / "_template.md",
        COPILOT_DIR / "agents" / "_template.agent.md",
    )
    for path in templates:
        assert "<!-- your agent description here -->" in path.read_text(), f"{path.name}: no slot"


def test_copilot_credential_names_start_with_the_required_prefix() -> None:
    text = (COPILOT_DIR / "mcp" / "repository-settings.json").read_text()
    names = re.findall(r"\$\{?([A-Z][A-Z0-9_]*)\}?", text)
    assert names, "repository-settings.json must reference its token by name"
    for name in names:
        assert name.startswith("COPILOT_MCP_"), f"{name}: Copilot only exposes COPILOT_MCP_*"


# --- secrets: two-sided scan ------------------------------------------------------------------


def test_no_secret_markers_anywhere_in_the_shipped_trees() -> None:
    for path in _shipped_files():
        lowered = path.read_text().lower()
        for marker in SECRET_MARKERS:
            assert marker not in lowered, f"{path}: marker {marker!r} found"


def _authorization_values(node: object) -> list[str]:
    values: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if str(key).lower() == "authorization":
                assert isinstance(value, str)
                values.append(value)
            else:
                values.extend(_authorization_values(value))
    elif isinstance(node, list):
        for item in node:
            values.extend(_authorization_values(item))
    return values


def test_every_auth_header_value_is_a_reference_not_a_literal() -> None:
    values: list[tuple[Path, str]] = []
    for path in _shipped_files():
        if path.suffix == ".json":
            for value in _authorization_values(json.loads(path.read_text())):
                values.append((path, value))
    # two-sided: the scan must actually see all three MCP configs, or it proves nothing
    assert len(values) >= 3, "expected auth headers in opencode.json + both .copilot/mcp configs"
    for path, value in values:
        assert AUTH_REFERENCE_RE.fullmatch(value), f"{path}: non-reference auth value {value!r}"
