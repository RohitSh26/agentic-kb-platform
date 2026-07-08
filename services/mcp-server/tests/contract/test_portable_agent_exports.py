"""The .copilot/ and .opencode/ renderings stay parity-pinned to agents/ (ADR-0009).

The canonical manifests in agents/ are the source of truth for tool access,
budgets, and instruction content. The host-native renderings are hand-authored,
so these contract tests are what keep them honest. The pinning model is
**pinned minimum + whatever exists**: the framework's six roles and three
skills must always be present, and every manifest *discovered* in agents/ —
including agents an adopting team adds later — must pass the same parity
checks: tool lists exact-match the canonical allowed_tools, budgets/evidence
rules/output schemas appear in every rendered body, host validity rules hold,
and no shipped file ever carries a token value
(contract: docs/contracts/portable-agent-framework.md).

Adopters who copy only the trees get the same verification from the shipped
standalone checker, agents/check_parity.py — smoke-tested here by subprocess
(never imported: services do not import root files, ADR-0008).
"""

import ast
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
AGENTS_DIR = REPO_ROOT / "agents"
OPENCODE_DIR = REPO_ROOT / ".opencode"
COPILOT_DIR = REPO_ROOT / ".copilot"

# the framework minimum: these must exist; teams may add more next to them
PINNED_ROLES = (
    "orchestrator",
    "implementation",
    "test_layer",
    "code_reviewer",
    "delivery_planner",
    "pr_planner",
)
PINNED_SPECIALISTS = tuple(role for role in PINNED_ROLES if role != "orchestrator")
PINNED_SKILLS = ("kb-first-file-fallback", "evidence-citation")
OPENCODE_SKILL_NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
COPILOT_MAX_BODY_CHARS = 30_000
SECRET_MARKERS = ("ghp_", "github_pat_", "secret")

# ADR-0025: only kb_search is broker-mediated (budgeted, server-enforced); the other five
# canonical tools are host-native filesystem primitives restored directly to the agent. Kept in
# lockstep with agents/check_parity.py's copy (asserted below) since services do not import root
# files (ADR-0008).
OPENCODE_NATIVE_TOOLS = {
    "read_file": "read",
    "read_full": "read",
    "list_files": "list",
    "grep": "grep",
    "edit_file": "edit",
}
COPILOT_NATIVE_TOOLS = {
    "read_file": "read",
    "read_full": "read",
    "list_files": "search",  # Copilot's `search` alias covers both Grep and Glob
    "grep": "search",
    "edit_file": "edit",
}
NATIVE_TOOLS = frozenset(OPENCODE_NATIVE_TOOLS)
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


def _checker_module_constants() -> dict[str, object]:
    """Extract check_parity.py's module-level constants without importing it.

    ADR-0008 forbids services importing root files, and the checker duplicates these
    constants on purpose. ast.literal_eval keeps the comparison value-based (not source
    text), so reformatting the checker never trips this — only a real value drift does.
    """
    tree = ast.parse((AGENTS_DIR / "check_parity.py").read_text())
    values: dict[str, object] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        source = node.value.args[0] if isinstance(node.value, ast.Call) else node.value
        try:
            values[target.id] = ast.literal_eval(source)
        except ValueError:
            continue
    return values


def test_checker_constants_match_the_contract_test_copies() -> None:
    # the parser and these constants are duplicated between this suite and
    # agents/check_parity.py (ADR-0008: the test cannot import the checker); pin that
    # the two copies agree so a fix in one can't silently diverge from the other
    constants = _checker_module_constants()
    assert constants["COPILOT_MAX_BODY_CHARS"] == COPILOT_MAX_BODY_CHARS
    assert constants["SECRET_MARKERS"] == SECRET_MARKERS
    assert constants["OPENCODE_SKILL_NAME_RE"] == OPENCODE_SKILL_NAME_RE.pattern
    assert constants["AUTH_REFERENCE_RE"] == AUTH_REFERENCE_RE.pattern
    assert constants["OPENCODE_NATIVE_TOOLS"] == OPENCODE_NATIVE_TOOLS
    assert constants["COPILOT_NATIVE_TOOLS"] == COPILOT_NATIVE_TOOLS


def _split(path: Path) -> tuple[list[str], str]:
    lines = path.read_text().splitlines()
    assert lines and lines[0] == "---", f"{path}: must start with frontmatter"
    for i, line in enumerate(lines[1:], start=1):
        if line == "---":
            return lines[1:i], "\n".join(lines[i + 1 :])
    raise AssertionError(f"{path}: unterminated frontmatter")


def _indent_of(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _parse_scalar(value: str) -> object:
    if value.startswith("[") and value.endswith("]"):
        return [item.strip().strip("'\"") for item in value[1:-1].split(",") if item.strip()]
    return value


def _parse_map(
    lines: list[str], start: int, indent: int, path: Path
) -> tuple[dict[str, object], int]:
    result: dict[str, object] = {}
    i = start
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        if _indent_of(line) < indent:
            break
        assert _indent_of(line) == indent, f"{path}: unexpected indent {line!r}"
        stripped = line.strip()
        assert not stripped.startswith("- "), f"{path}: list item where a map entry was expected"
        key, sep, raw = stripped.partition(":")
        assert sep, f"{path}: bad line {line!r}"
        value = raw.strip()
        if value:
            result[key.strip().strip("'\"")] = _parse_scalar(value)
            i += 1
        else:
            result[key.strip().strip("'\"")], i = _parse_block(lines, i + 1, indent, path)
    return result, i


def _parse_list(lines: list[str], start: int, indent: int, path: Path) -> tuple[list[object], int]:
    items: list[object] = []
    i = start
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        if _indent_of(line) != indent or not line.strip().startswith("- "):
            break
        head = line.strip().removeprefix("- ").strip()
        i += 1
        if ": " not in head:
            items.append(head)
            continue
        key, _, raw = head.partition(":")
        item: dict[str, object] = {key.strip(): _parse_scalar(raw.strip())}
        if (
            i < len(lines)
            and lines[i].strip()
            and _indent_of(lines[i]) > indent
            and not lines[i].strip().startswith("- ")
        ):
            extra, i = _parse_map(lines, i, _indent_of(lines[i]), path)
            item.update(extra)
        items.append(item)
    return items, i


def _parse_block(
    lines: list[str], start: int, parent_indent: int, path: Path
) -> tuple[object, int]:
    i = start
    if i >= len(lines) or not lines[i].strip() or _indent_of(lines[i]) <= parent_indent:
        return "", i
    child_indent = _indent_of(lines[i])
    if lines[i].strip().startswith("- "):
        return _parse_list(lines, i, child_indent, path)
    return _parse_map(lines, i, child_indent, path)


def _parse_fields(lines: list[str], path: Path) -> dict[str, object]:
    """Hand-rolled frontmatter parsing (mcp-server has no pyyaml; do not add one).

    Handles the shapes the canon and renderings use: ``key: value``, inline flow
    lists (``key: ['a', 'b']``), block lists (``- item``), block maps, nested
    block maps (``permission.task``), and lists of block maps (``handoffs``).
    """
    fields, consumed = _parse_map(lines, 0, 0, path)
    assert consumed == len(lines), f"{path}: unparsed frontmatter from line {consumed + 1}"
    return fields


def _read(path: Path) -> tuple[dict[str, object], str]:
    frontmatter, body = _split(path)
    return _parse_fields(frontmatter, path), body


def _discovered_roles() -> tuple[str, ...]:
    roles = tuple(
        sorted(
            p.stem
            for p in AGENTS_DIR.glob("*.md")
            if p.name != "README.md" and not p.name.startswith("_")
        )
    )
    assert roles, "no agent manifests discovered"
    return roles


def _discovered_skills() -> tuple[str, ...]:
    return tuple(sorted(p.name for p in (OPENCODE_DIR / "skills").iterdir() if p.is_dir()))


def _canon(role: str) -> tuple[dict[str, object], str]:
    return _read(AGENTS_DIR / f"{role}.md")


def _canon_tools(role: str) -> list[str]:
    tools = _canon(role)[0]["allowed_tools"]
    assert isinstance(tools, list) and tools
    return tools


def _is_orchestrator(role: str) -> bool:
    """ADR-0025 discriminator for 'may this agent launch subagents / run mode primary'.
    context.create_pack no longer exists; requires_human_approval: true is its structural
    replacement (today set only on agents/orchestrator.md's canon)."""
    return _canon(role)[0].get("requires_human_approval") == "true"


def _opencode_tool(tool: str) -> str:
    return OPENCODE_NATIVE_TOOLS.get(tool, f"context-broker_{tool.replace('.', '_')}")


def _copilot_tool(tool: str) -> str:
    return COPILOT_NATIVE_TOOLS.get(tool, f"context-broker/{tool.replace('.', '_')}")


def _dedupe(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


def _opencode_agent(role: str) -> tuple[dict[str, object], str]:
    return _read(OPENCODE_DIR / "agents" / f"{role}.md")


def _copilot_agent(role: str) -> tuple[dict[str, object], str]:
    return _read(COPILOT_DIR / "agents" / f"{role}.agent.md")


def _rendered_bodies(role: str) -> dict[str, str]:
    return {"opencode": _opencode_agent(role)[1], "copilot": _copilot_agent(role)[1]}


def _shipped_files() -> list[Path]:
    skip_dirs = {"node_modules", ".venv", "__pycache__", ".git"}  # vendored/generated,
    # not shipped policy — opencode materializes node_modules when opened in this repo
    files = [
        path
        for tree in (OPENCODE_DIR, COPILOT_DIR)
        for path in sorted(tree.rglob("*"))
        if path.is_file() and not skip_dirs.intersection(path.parts)
    ]
    assert files, "renderings missing"
    return files


# --- the framework minimum is pinned ----------------------------------------------------------


def test_the_six_pinned_roles_and_two_pinned_skills_always_exist() -> None:
    assert set(PINNED_ROLES) <= set(_discovered_roles()), "a framework role was removed"
    assert set(PINNED_SKILLS) <= set(_discovered_skills()), "a framework skill was removed"


# --- parity: every discovered rendering preserves the canon ------------------------------------


def test_every_discovered_manifest_exists_in_both_renderings_plus_templates() -> None:
    for role in _discovered_roles():
        assert (OPENCODE_DIR / "agents" / f"{role}.md").is_file(), f"opencode missing {role}"
        assert (COPILOT_DIR / "agents" / f"{role}.agent.md").is_file(), f"copilot missing {role}"
    assert (OPENCODE_DIR / "agents" / "_template.md").is_file()
    assert (COPILOT_DIR / "agents" / "_template.agent.md").is_file()


def test_opencode_frontmatter_tools_exactly_match_canonical_allowed_tools() -> None:
    for role in _discovered_roles():
        expected = _dedupe([_opencode_tool(tool) for tool in _canon_tools(role)])
        tools = _opencode_agent(role)[0]["tools"]
        assert isinstance(tools, dict), f"{role}: opencode tools must be a map"
        assert list(tools) == expected, f"{role}: opencode tools drifted from canon"
        assert all(v == "true" for v in tools.values()), f"{role}: every entry must enable"


def test_opencode_json_per_agent_tools_exactly_match_canonical_allowed_tools() -> None:
    config = json.loads((OPENCODE_DIR / "opencode.json").read_text())
    # deny-by-default extends past the broker namespace to every ADR-0025 native tool name
    expected_global_off = {"context-broker_*": False} | {
        name: False for name in sorted(set(OPENCODE_NATIVE_TOOLS.values()))
    }
    assert config["tools"] == expected_global_off, "broker + native tools must default off"
    agents = config["agent"]
    assert set(agents) == set(_discovered_roles()), (
        "opencode.json agent entries must cover exactly the manifests in agents/"
    )
    for role in _discovered_roles():
        expected = {name: True for name in _dedupe([_opencode_tool(t) for t in _canon_tools(role)])}
        assert agents[role]["tools"] == expected, f"{role}: opencode.json drifted from canon"


def test_copilot_frontmatter_tools_exactly_match_canonical_allowed_tools() -> None:
    for role in _discovered_roles():
        fields, _ = _copilot_agent(role)
        expected = _dedupe([_copilot_tool(tool) for tool in _canon_tools(role)])
        if fields.get("agents"):
            # the `agents` field requires the host `agent` tool — the single pinned
            # exception to tool-parity (composition, not a data tool)
            expected.append("agent")
        assert fields["tools"] == expected, f"{role}: copilot tools drifted from canon"
        assert fields["name"] == _canon(role)[0]["name"], f"{role}: copilot name drifted"


def test_rendered_bodies_contain_the_canonical_instruction_body_verbatim() -> None:
    for role in _discovered_roles():
        canonical_body = _canon(role)[1].strip()
        for host, body in _rendered_bodies(role).items():
            assert canonical_body in body, f"{host}/{role}: canonical body not verbatim"


def test_budget_numbers_in_every_rendered_body_match_the_canon() -> None:
    for role in _discovered_roles():
        fields = _canon(role)[0]
        for host, body in _rendered_bodies(role).items():
            assert f"max_context_calls: {fields['max_context_calls']}" in body, f"{host}/{role}"
            assert f"max_context_tokens: {fields['max_context_tokens']}" in body, f"{host}/{role}"


def test_source_citation_rule_in_every_rendered_body() -> None:
    for role in _discovered_roles():
        for host, body in _rendered_bodies(role).items():
            lowered = body.lower()
            assert "cites a source" in lowered, f"{host}/{role}: source-citation rule missing"
            assert "open question" in lowered, f"{host}/{role}: open-question rule missing"


def test_kb_search_budget_discipline_in_every_rendered_body() -> None:
    # replaces the retired context.request_more five-field justification contract: kb_search
    # takes a plain query, so the equivalent discipline to pin is "budgeted, named, in every body"
    for role in _discovered_roles():
        for host, body in _rendered_bodies(role).items():
            assert "kb_search" in body, f"{host}/{role}: kb_search not named"
            assert "budget" in body.lower(), f"{host}/{role}: kb_search budget discipline missing"


def test_untrusted_content_rule_in_every_rendered_body() -> None:
    for role in _discovered_roles():
        for host, body in _rendered_bodies(role).items():
            assert "untrusted" in body.lower(), f"{host}/{role}: untrusted-content rule missing"


def test_output_schema_named_in_every_rendered_body() -> None:
    for role in _discovered_roles():
        schema = _canon(role)[0]["output_schema"]
        assert isinstance(schema, str)
        for host, body in _rendered_bodies(role).items():
            assert schema in body, f"{host}/{role}: output_schema {schema} not named"


def test_provenance_comment_in_every_rendered_body() -> None:
    for role in _discovered_roles():
        for host, body in _rendered_bodies(role).items():
            assert f"<!-- rendered from agents/{role}.md" in body, (
                f"{host}/{role}: provenance comment missing"
            )


def test_copilot_repository_settings_allowlist_is_the_union_of_canonical_tools() -> None:
    config = json.loads((COPILOT_DIR / "mcp" / "repository-settings.json").read_text())
    allowed = config["mcpServers"]["context-broker"]["tools"]
    # only MCP-routed tools belong here -- native host tools are never routed through the broker
    expected = sorted(
        {
            tool
            for role in _discovered_roles()
            for tool in _canon_tools(role)
            if tool not in NATIVE_TOOLS
        }
    )
    assert allowed != ["*"], "server-level allowlist must enumerate tools, not expose everything"
    assert sorted(allowed) == expected, "repository-settings allowlist drifted from canon union"


# --- composition: native subagent + skill declarations ---------------------------------------


def test_copilot_orchestrator_agents_field_includes_the_five_pinned_specialists() -> None:
    fields, _ = _copilot_agent("orchestrator")
    agents = fields["agents"]
    assert isinstance(agents, list)
    pinned = [_canon(role)[0]["name"] for role in PINNED_SPECIALISTS]
    assert set(pinned) <= set(agents), "a pinned specialist left the orchestrator's agents list"
    canonical_names = {_canon(role)[0]["name"] for role in _discovered_roles()}
    assert set(agents) <= canonical_names, "orchestrator declares a non-canonical subagent"


def test_copilot_non_orchestrators_and_template_declare_no_subagents() -> None:
    names = [f"{role}.agent.md" for role in _discovered_roles() if not _is_orchestrator(role)]
    for name in (*names, "_template.agent.md"):
        fields, _ = _read(COPILOT_DIR / "agents" / name)
        assert fields["agents"] == [], f"{name}: specialists never spawn — agents must be []"
        assert "handoffs" not in fields, f"{name}: handoffs are pack-creator-only"
        tools = fields["tools"]
        assert isinstance(tools, list) and "agent" not in tools, f"{name}: agent tool leaked"


def test_copilot_handoff_targets_are_the_declared_agents_and_do_not_autosend() -> None:
    for role in _discovered_roles():
        fields, _ = _copilot_agent(role)
        agents = fields.get("agents")
        if not agents:
            continue
        handoffs = fields["handoffs"]
        assert isinstance(handoffs, list)
        assert [h["agent"] for h in handoffs] == agents, f"{role}: handoff target not declared"
        for handoff in handoffs:
            assert handoff.get("label") and handoff.get("prompt"), "handoff needs label + prompt"
            assert handoff.get("send") == "false", "handoffs must not auto-send"


def test_opencode_orchestrator_task_permission_includes_the_five_pinned_specialists() -> None:
    permission = _opencode_agent("orchestrator")[0]["permission"]
    assert isinstance(permission, dict)
    task = permission["task"]
    assert isinstance(task, dict)
    assert task.get("*") == "deny", "task permission must deny by default"
    allowed = {key: value for key, value in task.items() if key != "*"}
    assert all(value == "allow" for value in allowed.values()), "non-allow task entry"
    assert set(PINNED_SPECIALISTS) <= set(allowed), "a pinned specialist left the task allowlist"
    assert set(allowed) <= set(_discovered_roles()), "task target is not a manifest in agents/"


def test_opencode_non_orchestrators_and_template_deny_all_task_launches() -> None:
    names = [role for role in _discovered_roles() if not _is_orchestrator(role)]
    for name in (*names, "_template"):
        fields, _ = _read(OPENCODE_DIR / "agents" / f"{name}.md")
        permission = fields["permission"]
        assert isinstance(permission, dict)
        assert permission["task"] == {"*": "deny"}, f"{name}: specialists never launch subagents"


def test_opencode_skill_permissions_deny_by_default_and_track_the_canonical_grants() -> None:
    for name in (*_discovered_roles(), "_template"):
        fields, _ = _read(OPENCODE_DIR / "agents" / f"{name}.md")
        permission = fields["permission"]
        assert isinstance(permission, dict)
        skill = permission["skill"]
        assert isinstance(skill, dict)
        assert skill.get("*") == "deny", f"{name}: skill permission must deny by default"
        allowed = [key for key, value in skill.items() if key != "*"]
        assert all(skill[key] == "allow" for key in allowed), f"{name}: non-allow skill entry"
        assert set(allowed) <= set(_discovered_skills()), f"{name}: allow-key not a shipped skill"
        assert "evidence-citation" in allowed, f"{name}: evidence-citation is framework-wide"


def test_kb_first_file_fallback_skill_tracks_the_kb_search_grant() -> None:
    # the ADR-0025 replacement for the retired evidence-pack-orchestration/context-request
    # -discipline split: one skill, gated on the one remaining budgeted tool (kb_search is
    # universal in the framework today, so this is a structural gate, not a coincidence)
    for name in (*_discovered_roles(), "_template"):
        fields, _ = _read(OPENCODE_DIR / "agents" / f"{name}.md")
        permission = fields["permission"]
        assert isinstance(permission, dict)
        skill = permission["skill"]
        assert isinstance(skill, dict)
        if name == "_template":
            tools = fields["tools"]
            assert isinstance(tools, dict)
            has_kb_search = "context-broker_kb_search" in tools
        else:
            has_kb_search = "kb_search" in _canon_tools(name)
        has_fallback_skill = skill.get("kb-first-file-fallback") == "allow"
        assert has_fallback_skill == has_kb_search, (
            f"{name}: kb-first-file-fallback must track the kb_search grant"
        )


# --- validity: each rendering is well-formed for its host ------------------------------------


def test_opencode_modes_are_primary_for_the_orchestrator_and_subagent_for_the_rest() -> None:
    for role in _discovered_roles():
        expected = "primary" if _is_orchestrator(role) else "subagent"
        assert _opencode_agent(role)[0]["mode"] == expected, f"{role}: wrong opencode mode"
    template, _ = _read(OPENCODE_DIR / "agents" / "_template.md")
    assert template["mode"] == "subagent"


def test_opencode_skills_follow_the_naming_rules() -> None:
    skills_dir = OPENCODE_DIR / "skills"
    for skill in _discovered_skills():
        fields, body = _read(skills_dir / skill / "SKILL.md")
        name = fields["name"]
        assert name == skill, f"{skill}: SKILL.md name must match its directory"
        assert isinstance(name, str) and OPENCODE_SKILL_NAME_RE.fullmatch(name), f"bad name {name}"
        assert len(name) <= 64
        description = fields["description"]
        assert isinstance(description, str) and description, f"{skill}: description required"
        assert len(description) <= 1024, f"{skill}: description over the documented limit"
        assert body.strip(), f"{skill}: empty skill body"


def test_copilot_skill_modules_mirror_the_opencode_skill_set() -> None:
    copilot_set = {p.name for p in (COPILOT_DIR / "skills").iterdir() if p.is_dir()}
    assert copilot_set == set(_discovered_skills()), "skill sets differ between renderings"
    for skill in _discovered_skills():
        path = COPILOT_DIR / "skills" / skill / "SKILL.md"
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
    assert len(paths) == 2 * (len(_discovered_roles()) + 1)
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
        lowered = path.read_text(errors="replace").lower()
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


# --- the shipped adopter checker agrees with this suite ---------------------------------------


def _run_checker(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(AGENTS_DIR / "check_parity.py"), "--repo-root", str(root)],
        capture_output=True,
        text=True,
        check=False,
    )


def _copy_trees(destination: Path) -> Path:
    for tree in ("agents", ".opencode", ".copilot"):
        shutil.copytree(REPO_ROOT / tree, destination / tree)
    return destination


def test_the_shipped_adopter_checker_passes_on_this_repo() -> None:
    result = _run_checker(REPO_ROOT)
    assert result.returncode == 0, f"checker failed on the repo:\n{result.stdout}{result.stderr}"
    assert "parity OK" in result.stdout


def test_the_checker_flags_tool_drift_and_literal_credentials(tmp_path: Path) -> None:
    root = _copy_trees(tmp_path)
    drifted = root / ".opencode" / "agents" / "code_reviewer.md"
    drifted.write_text(
        drifted.read_text().replace(
            "context-broker_kb_search: true",
            "context-broker_context_create_pack: true",
        )
    )
    literal = root / ".copilot" / "mcp" / "vscode-mcp.json"
    literal.write_text(literal.read_text().replace("${input:context-broker-token}", "abc123"))
    result = _run_checker(root)
    assert result.returncode == 1
    assert "code_reviewer.md" in result.stderr, "tool drift not reported"
    assert "vscode-mcp.json" in result.stderr, "literal credential not reported"


def test_the_checker_accepts_a_team_added_agent(tmp_path: Path) -> None:
    """The extensibility promise: one more agent, rendered in parity, passes everything.

    Asserted against a count computed from the live roster rather than a hardcoded number:
    the roster itself is discovery-driven and has already grown once (ADR-0030, 6 -> 12
    roles) since this test was written -- a literal count would drift again next time.
    """
    root = _copy_trees(tmp_path)
    renames = (("code_reviewer", "risk_auditor"), ("code_reviewer_agent", "risk_auditor_agent"))

    def rendered(source: Path) -> str:
        text = source.read_text()
        for old, new in renames:
            text = text.replace(old, new)
        return text

    (root / "agents" / "risk_auditor.md").write_text(rendered(AGENTS_DIR / "code_reviewer.md"))
    (root / ".opencode" / "agents" / "risk_auditor.md").write_text(
        rendered(OPENCODE_DIR / "agents" / "code_reviewer.md")
    )
    (root / ".copilot" / "agents" / "risk_auditor.agent.md").write_text(
        rendered(COPILOT_DIR / "agents" / "code_reviewer.agent.md")
    )
    config_path = root / ".opencode" / "opencode.json"
    config = json.loads(config_path.read_text())
    config["agent"]["risk_auditor"] = config["agent"]["code_reviewer"]
    config_path.write_text(json.dumps(config, indent=2))
    result = _run_checker(root)
    assert result.returncode == 0, f"team-added agent rejected:\n{result.stdout}{result.stderr}"
    assert f"{len(_discovered_roles()) + 1} agent(s)" in result.stdout
