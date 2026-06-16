#!/usr/bin/env python3
"""Adopter-side parity checker for the portable agent framework (ADR-0009).

Copy this file along with `agents/`, `.copilot/`, and `.opencode/` into your
repository and run it (locally or in CI):

    python agents/check_parity.py [--repo-root PATH]

It is discovery-driven: every manifest found in `agents/*.md` — the framework's
six roles and any agents your team adds — is checked against the parity
checklist in docs/contracts/portable-agent-framework.md:

- tool parity: each rendering grants exactly the canon's allowed_tools
  (opencode agent file + opencode.json override; copilot agent file +
  repository-settings union), broker namespace deny-by-default;
- body parity: the canonical instruction body verbatim, the budget lines,
  the evidence-ID / request-more / untrusted-content rules, the output
  schema name, and the rendering provenance comment;
- composition: deny-by-default `permission` blocks, subagent launching only
  for pack-creating agents, copilot handoffs consistent with `agents`, and
  skill grants that track the canon (`context-request-discipline` iff
  `context.request_more`, `evidence-pack-orchestration` iff
  `context.create_pack`, `evidence-citation` everywhere);
- host validity: opencode modes and skill naming rules, copilot's 30k body
  cap, template description slots;
- secrets: no credential markers anywhere, every Authorization value a
  reference (never a literal), and the known MCP configs actually scanned.

Exit 0 when everything found is parity-clean; exit 1 with one line per
failure otherwise. Stdlib only — no installs needed.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

OPENCODE_SKILL_NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
COPILOT_MAX_BODY_CHARS = 30_000
REQUEST_MORE_FIELDS = ("question", "why_needed", "decision_needed", "already_checked", "max_tokens")
SECRET_MARKERS = ("ghp_", "github_pat_", "secret")
AUTH_REFERENCE_RE = re.compile(
    r"^Bearer ("
    r"\$COPILOT_MCP_[A-Z0-9_]+"
    r"|\$\{COPILOT_MCP_[A-Z0-9_]+\}"
    r"|\$\{input:[a-z0-9-]+\}"
    r"|\{env:[A-Z0-9_]+\}"
    r")$"
)


class ParseError(Exception):
    pass


# --- minimal frontmatter parsing (the shapes the canon and renderings use; no pyyaml) ----------


def _split(path: Path) -> tuple[list[str], str]:
    try:
        lines = path.read_text().splitlines()
    except (UnicodeDecodeError, OSError) as error:
        raise ParseError(f"{path}: unreadable ({error})") from error
    if not lines or lines[0] != "---":
        raise ParseError(f"{path}: must start with frontmatter")
    for i, line in enumerate(lines[1:], start=1):
        if line == "---":
            return lines[1:i], "\n".join(lines[i + 1 :])
    raise ParseError(f"{path}: unterminated frontmatter")


def _indent_of(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _parse_scalar(value: str) -> object:
    if value.startswith("[") and value.endswith("]"):
        return [item.strip().strip("'\"") for item in value[1:-1].split(",") if item.strip()]
    return value


def _parse_map(lines: list[str], start: int, indent: int, path: Path) -> tuple[dict, int]:
    result: dict[str, object] = {}
    i = start
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        if _indent_of(line) < indent:
            break
        if _indent_of(line) != indent or line.strip().startswith("- "):
            raise ParseError(f"{path}: unexpected line {line!r}")
        key, sep, raw = line.strip().partition(":")
        if not sep:
            raise ParseError(f"{path}: bad line {line!r}")
        value = raw.strip()
        if value:
            result[key.strip().strip("'\"")] = _parse_scalar(value)
            i += 1
        else:
            result[key.strip().strip("'\"")], i = _parse_block(lines, i + 1, indent, path)
    return result, i


def _parse_list(lines: list[str], start: int, indent: int, path: Path) -> tuple[list, int]:
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


def _read(path: Path) -> tuple[dict, str]:
    frontmatter, body = _split(path)
    fields, consumed = _parse_map(frontmatter, 0, 0, path)
    if consumed != len(frontmatter):
        raise ParseError(f"{path}: unparsed frontmatter from line {consumed + 1}")
    return fields, body


# --- the checker -------------------------------------------------------------------------------


class ParityChecker:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.agents_dir = root / "agents"
        self.opencode_dir = root / ".opencode"
        self.copilot_dir = root / ".copilot"
        self.failures: list[str] = []
        self.files_scanned = 0

    def fail(self, message: str) -> None:
        self.failures.append(message)

    def load_json(self, path: Path) -> object | None:
        """Adopters hand-edit these trees: report bad files, never crash on them."""
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, UnicodeDecodeError, OSError) as error:
            self.fail(f"{path.relative_to(self.root)}: unreadable JSON ({error})")
            return None

    def discovered_roles(self) -> list[str]:
        return sorted(
            p.stem
            for p in self.agents_dir.glob("*.md")
            if p.name != "README.md" and not p.name.startswith("_")
        )

    def discovered_skills(self) -> list[str]:
        # anchored to the OpenCode tree (the shipped skill names): per-agent skill
        # membership checks against this set, and check_skills separately pins that the
        # copilot tree carries the same set, so the two definitions cannot diverge
        opencode_skills = self.opencode_dir / "skills"
        if not opencode_skills.is_dir():
            return []
        return sorted(p.name for p in opencode_skills.iterdir() if p.is_dir())

    def canon(self, role: str) -> tuple[dict, str] | None:
        try:
            fields, body = _read(self.agents_dir / f"{role}.md")
        except ParseError as error:
            self.fail(str(error))
            return None
        for required in (
            "name",
            "allowed_tools",
            "max_context_calls",
            "max_context_tokens",
            "output_schema",
        ):
            if required not in fields:
                self.fail(f"agents/{role}.md: missing canonical field {required!r}")
                return None
        tools = fields["allowed_tools"]
        if not isinstance(tools, list) or not tools:
            self.fail(f"agents/{role}.md: allowed_tools must be a non-empty list")
            return None
        return fields, body

    # --- per-role checks --------------------------------------------------------------------

    def check_body(
        self, label: str, canon_fields: dict, canon_body: str, role: str, body: str
    ) -> None:
        if canon_body.strip() not in body:
            self.fail(f"{label}: canonical instruction body not reproduced verbatim")
        for line in (
            f"max_context_calls: {canon_fields['max_context_calls']}",
            f"max_context_tokens: {canon_fields['max_context_tokens']}",
        ):
            if line not in body:
                self.fail(f"{label}: budget line {line!r} missing from the body")
        lowered = body.lower()
        if "evidence id" not in lowered or "open question" not in lowered:
            self.fail(f"{label}: evidence-ID / open-question rule missing")
        for field in REQUEST_MORE_FIELDS:
            if field not in body:
                self.fail(f"{label}: request_more field {field!r} missing")
        if "untrusted" not in lowered:
            self.fail(f"{label}: untrusted-content rule missing")
        if str(canon_fields["output_schema"]) not in body:
            self.fail(f"{label}: output_schema {canon_fields['output_schema']!r} not named")
        if f"<!-- rendered from agents/{role}.md" not in body:
            self.fail(f"{label}: rendering provenance comment missing")

    def check_opencode_agent(
        self, role: str, canon_fields: dict, canon_body: str, roles: list[str], skills: list[str]
    ) -> None:
        path = self.opencode_dir / "agents" / f"{role}.md"
        label = f".opencode/agents/{role}.md"
        if not path.is_file():
            self.fail(f"{label}: rendering missing for agents/{role}.md")
            return
        try:
            fields, body = _read(path)
        except ParseError as error:
            self.fail(str(error))
            return
        canon_tools = list(canon_fields["allowed_tools"])
        if not isinstance(fields.get("description"), str) or not fields.get("description"):
            self.fail(f"{label}: description required")
        creates_packs = "context.create_pack" in canon_tools
        expected_mode = "primary" if creates_packs else "subagent"
        if fields.get("mode") != expected_mode:
            self.fail(f"{label}: mode must be {expected_mode!r} (create_pack grant decides)")
        tools = fields.get("tools")
        expected = [f"context-broker_{tool.replace('.', '_')}" for tool in canon_tools]
        if not isinstance(tools, dict) or list(tools) != expected:
            self.fail(f"{label}: tools map drifted from canonical allowed_tools")
        if isinstance(tools, dict) and any(value != "true" for value in tools.values()):
            # independent of the set check above, so both are collected in one run
            self.fail(f"{label}: every tools entry must enable (true)")
        permission = fields.get("permission")
        if not isinstance(permission, dict):
            self.fail(f"{label}: permission block required (deny-by-default composition)")
        else:
            self.check_opencode_permission(label, permission, canon_tools, roles, skills)
        self.check_body(label, canon_fields, canon_body, role, body)

    def check_opencode_permission(
        self,
        label: str,
        permission: dict,
        canon_tools: list[str],
        roles: list[str],
        skills: list[str],
    ) -> None:
        creates_packs = "context.create_pack" in canon_tools
        task = permission.get("task")
        if not isinstance(task, dict) or task.get("*") != "deny":
            self.fail(f"{label}: permission.task must deny '*' by default")
        else:
            launchable = [key for key in task if key != "*"]
            if launchable and not creates_packs:
                self.fail(f"{label}: only pack-creating agents may launch subagents")
            for target in launchable:
                if task[target] != "allow":
                    self.fail(f"{label}: permission.task[{target!r}] must be allow or absent")
                if target not in roles:
                    self.fail(f"{label}: task target {target!r} is not a manifest in agents/")
        skill = permission.get("skill")
        if not isinstance(skill, dict) or skill.get("*") != "deny":
            self.fail(f"{label}: permission.skill must deny '*' by default")
        else:
            allowed = [key for key in skill if key != "*"]
            for name in allowed:
                if skill[name] != "allow":
                    self.fail(f"{label}: permission.skill[{name!r}] must be allow or absent")
                if name not in skills:
                    self.fail(f"{label}: skill {name!r} is not shipped in the skills trees")
            if ("context-request-discipline" in allowed) != ("context.request_more" in canon_tools):
                self.fail(f"{label}: context-request-discipline must track the request_more grant")
            if ("evidence-pack-orchestration" in allowed) != creates_packs:
                self.fail(f"{label}: evidence-pack-orchestration must track the create_pack grant")
            if "evidence-citation" not in allowed:
                self.fail(f"{label}: evidence-citation must be allowed for every agent")

    def check_copilot_agent(
        self, role: str, canon_fields: dict, canon_body: str, canon_names: dict[str, str]
    ) -> None:
        path = self.copilot_dir / "agents" / f"{role}.agent.md"
        label = f".copilot/agents/{role}.agent.md"
        if not path.is_file():
            self.fail(f"{label}: rendering missing for agents/{role}.md")
            return
        try:
            fields, body = _read(path)
        except ParseError as error:
            self.fail(str(error))
            return
        canon_tools = list(canon_fields["allowed_tools"])
        if fields.get("name") != canon_fields["name"]:
            self.fail(f"{label}: name drifted from the canon")
        if not isinstance(fields.get("description"), str) or not fields.get("description"):
            self.fail(f"{label}: description required")
        if "mcp-servers" in fields:
            self.fail(
                f"{label}: no mcp-servers block in frontmatter — connection ships in .copilot/mcp/"
            )
        agents = fields.get("agents")
        if not isinstance(agents, list):
            self.fail(f"{label}: agents field required (empty list for specialists)")
            agents = []
        creates_packs = "context.create_pack" in canon_tools
        if agents and not creates_packs:
            self.fail(f"{label}: only pack-creating agents may declare subagents")
        for target in agents:
            if target not in canon_names.values():
                self.fail(f"{label}: subagent {target!r} is not a canonical agent name")
        expected = [f"context-broker/{tool.replace('.', '_')}" for tool in canon_tools]
        if agents:
            expected = [*expected, "agent"]
        if fields.get("tools") != expected:
            self.fail(f"{label}: tools list drifted from canonical allowed_tools")
        # handoffs are VS Code-only and optional (the cloud agent ignores them); validate
        # their shape only when present rather than requiring them whenever agents exist
        handoffs = fields.get("handoffs")
        if handoffs is not None:
            if not agents:
                self.fail(f"{label}: handoffs without declared agents")
            elif not isinstance(handoffs, list) or [h.get("agent") for h in handoffs] != agents:
                self.fail(f"{label}: handoffs must target exactly the declared agents, in order")
            else:
                for handoff in handoffs:
                    if not handoff.get("label") or not handoff.get("prompt"):
                        self.fail(f"{label}: every handoff needs label + prompt")
                    if handoff.get("send") != "false":
                        self.fail(f"{label}: handoffs must not auto-send (send: false)")
        if len(body) >= COPILOT_MAX_BODY_CHARS:
            self.fail(f"{label}: body over Copilot's 30k character limit")
        self.check_body(label, canon_fields, canon_body, role, body)

    # --- tree-level checks ------------------------------------------------------------------

    def check_opencode_json(
        self, roles: list[str], canon_tools_by_role: dict[str, list[str]]
    ) -> None:
        path = self.opencode_dir / "opencode.json"
        if not path.is_file():
            self.fail(".opencode/opencode.json: missing")
            return
        config = self.load_json(path)
        if not isinstance(config, dict):
            return
        if config.get("tools") != {"context-broker_*": False}:
            self.fail(
                ".opencode/opencode.json: broker namespace must default off (context-broker_*: false)"
            )
        agents = config.get("agent", {})
        if set(agents) != set(roles):
            self.fail(
                ".opencode/opencode.json: agent entries must cover exactly the manifests in agents/ "
                f"(config: {sorted(agents)}, agents/: {roles})"
            )
        for role in roles:
            # a role whose canon failed to parse has no tool list to compare against
            if role not in agents or role not in canon_tools_by_role:
                continue
            expected = {f"context-broker_{tool.replace('.', '_')}": True for tool in canon_tools_by_role[role]}
            if agents[role].get("tools") != expected:
                self.fail(f".opencode/opencode.json: agent[{role!r}].tools drifted from canon")

    def check_repository_settings(self, canon_tools_by_role: dict[str, list[str]]) -> None:
        path = self.copilot_dir / "mcp" / "repository-settings.json"
        if not path.is_file():
            self.fail(".copilot/mcp/repository-settings.json: missing")
            return
        config = self.load_json(path)
        if not isinstance(config, dict):
            return
        allowed = config.get("mcpServers", {}).get("context-broker", {}).get("tools")
        if allowed == ["*"]:
            self.fail(
                ".copilot/mcp/repository-settings.json: allowlist must enumerate tools, not '*'"
            )
            return
        expected = sorted({tool for tools in canon_tools_by_role.values() for tool in tools})
        if not isinstance(allowed, list) or sorted(allowed) != expected:
            self.fail(
                ".copilot/mcp/repository-settings.json: allowlist drifted from the canon union"
            )

    def check_skills(self, skills: list[str]) -> None:
        opencode_skills = self.opencode_dir / "skills"
        copilot_skills = self.copilot_dir / "skills"
        opencode_set = (
            {p.name for p in opencode_skills.iterdir() if p.is_dir()}
            if opencode_skills.is_dir()
            else set()
        )
        copilot_set = (
            {p.stem for p in copilot_skills.glob("*.md")} if copilot_skills.is_dir() else set()
        )
        if self.opencode_dir.is_dir() and self.copilot_dir.is_dir() and opencode_set != copilot_set:
            self.fail(
                f"skills differ between renderings (opencode: {sorted(opencode_set)}, copilot: {sorted(copilot_set)})"
            )
        for skill in sorted(opencode_set):
            path = opencode_skills / skill / "SKILL.md"
            label = f".opencode/skills/{skill}/SKILL.md"
            if not path.is_file():
                self.fail(f"{label}: missing")
                continue
            try:
                fields, body = _read(path)
            except ParseError as error:
                self.fail(str(error))
                continue
            name = fields.get("name")
            if name != skill:
                self.fail(f"{label}: name must match its directory")
            if (
                not isinstance(name, str)
                or not OPENCODE_SKILL_NAME_RE.fullmatch(name)
                or len(name) > 64
            ):
                self.fail(f"{label}: name breaks OpenCode skill naming rules")
            description = fields.get("description")
            if not isinstance(description, str) or not description or len(description) > 1024:
                self.fail(f"{label}: description required, max 1024 chars")
            if not body.strip():
                self.fail(f"{label}: empty skill body")
        for skill in sorted(copilot_set):
            path = copilot_skills / f"{skill}.md"
            try:
                text = path.read_text()
            except (UnicodeDecodeError, OSError) as error:
                self.fail(f".copilot/skills/{skill}.md: unreadable ({error})")
                continue
            if not text.strip():
                self.fail(f".copilot/skills/{skill}.md: empty skill module")
        # body parity: the two host renderings of a skill must not drift. There is no
        # canonical source yet (a recorded follow-up); this is the interim gate over the
        # safety-critical prose. The opencode body is its post-frontmatter content.
        for skill in sorted(opencode_set & copilot_set):
            try:
                _, opencode_body = _read(opencode_skills / skill / "SKILL.md")
                copilot_text = (copilot_skills / f"{skill}.md").read_text(errors="replace")
            except (ParseError, OSError):
                continue  # the per-file checks above already reported the unreadable file
            if opencode_body.strip() != copilot_text.strip():
                self.fail(f"skill {skill!r}: opencode and copilot renderings have drifted")

    def check_templates(self) -> None:
        slot = "<!-- your agent description here -->"
        opencode_template = self.opencode_dir / "agents" / "_template.md"
        if self.opencode_dir.is_dir():
            if not opencode_template.is_file():
                self.fail(".opencode/agents/_template.md: missing")
            else:
                try:
                    fields, _ = _read(opencode_template)
                except ParseError as error:
                    self.fail(str(error))
                else:
                    if fields.get("mode") != "subagent":
                        self.fail(".opencode/agents/_template.md: template mode must be subagent")
                    permission = fields.get("permission")
                    if not isinstance(permission, dict) or permission.get("task") != {"*": "deny"}:
                        self.fail(
                            ".opencode/agents/_template.md: template must deny all task launches"
                        )
                    skill = permission.get("skill") if isinstance(permission, dict) else None
                    if not isinstance(skill, dict) or skill.get("*") != "deny":
                        self.fail(
                            ".opencode/agents/_template.md: template skill permission must "
                            "deny '*' by default"
                        )
                if slot not in opencode_template.read_text(errors="replace"):
                    self.fail(".opencode/agents/_template.md: description slot comment missing")
        copilot_template = self.copilot_dir / "agents" / "_template.agent.md"
        if self.copilot_dir.is_dir():
            if not copilot_template.is_file():
                self.fail(".copilot/agents/_template.agent.md: missing")
            else:
                try:
                    fields, body = _read(copilot_template)
                except ParseError as error:
                    self.fail(str(error))
                else:
                    if fields.get("agents") != []:
                        self.fail(
                            ".copilot/agents/_template.agent.md: template must declare agents: []"
                        )
                    if "handoffs" in fields:
                        self.fail(
                            ".copilot/agents/_template.agent.md: handoffs are orchestrator-only"
                        )
                    tools = fields.get("tools")
                    if isinstance(tools, list) and "agent" in tools:
                        self.fail(
                            ".copilot/agents/_template.agent.md: agent tool is pack-creator-only"
                        )
                    if len(body) >= COPILOT_MAX_BODY_CHARS:
                        self.fail(
                            ".copilot/agents/_template.agent.md: body over Copilot's 30k "
                            "character limit"
                        )
                if slot not in copilot_template.read_text(errors="replace"):
                    self.fail(
                        ".copilot/agents/_template.agent.md: description slot comment missing"
                    )

    def _authorization_values(self, node: object) -> list[str]:
        values: list[str] = []
        if isinstance(node, dict):
            for key, value in node.items():
                if str(key).lower() == "authorization" and isinstance(value, str):
                    values.append(value)
                else:
                    values.extend(self._authorization_values(value))
        elif isinstance(node, list):
            for item in node:
                values.extend(self._authorization_values(item))
        return values

    def check_secrets(self) -> None:
        known_configs = (
            self.opencode_dir / "opencode.json",
            self.copilot_dir / "mcp" / "repository-settings.json",
            self.copilot_dir / "mcp" / "vscode-mcp.json",
        )
        parsed: dict[Path, object | None] = {}
        for tree in (self.opencode_dir, self.copilot_dir):
            if not tree.is_dir():
                continue
            for path in sorted(tree.rglob("*")):
                if not path.is_file():
                    continue
                self.files_scanned += 1
                # errors="replace" keeps the marker scan working on non-UTF-8 files
                lowered = path.read_text(errors="replace").lower()
                for marker in SECRET_MARKERS:
                    if marker in lowered:
                        self.fail(
                            f"{path.relative_to(self.root)}: credential marker {marker!r} found"
                        )
                if path.suffix == ".json":
                    config = self.load_json(path)
                    parsed[path] = config
                    for value in self._authorization_values(config):
                        if not AUTH_REFERENCE_RE.fullmatch(value):
                            self.fail(
                                f"{path.relative_to(self.root)}: Authorization value is not a "
                                f"reference: {value!r}"
                            )
        # two-sided: each shipped MCP config must actually carry an auth reference to scan.
        # reuse the parse from the scan above — an unreadable file already failed once, so
        # do not re-parse it or stack a confusing "no reference" failure on the same file
        for path in known_configs:
            config = parsed.get(path)
            if path.is_file() and config is not None and not self._authorization_values(config):
                self.fail(
                    f"{path.relative_to(self.root)}: no Authorization reference found to verify"
                )

    # --- entry point --------------------------------------------------------------------------

    def run(self) -> int:
        if not self.agents_dir.is_dir():
            print(f"FAIL: no agents/ directory under {self.root}", file=sys.stderr)
            return 1
        if not self.opencode_dir.is_dir() and not self.copilot_dir.is_dir():
            print(
                f"FAIL: neither .opencode/ nor .copilot/ found under {self.root}", file=sys.stderr
            )
            return 1
        roles = self.discovered_roles()
        skills = self.discovered_skills()
        if not roles:
            print(f"FAIL: no agent manifests in {self.agents_dir}", file=sys.stderr)
            return 1
        canon_tools_by_role: dict[str, list[str]] = {}
        canon_names: dict[str, str] = {}
        parsed: dict[str, tuple[dict, str]] = {}
        for role in roles:
            canon = self.canon(role)
            if canon is None:
                continue
            parsed[role] = canon
            canon_tools_by_role[role] = list(canon[0]["allowed_tools"])
            canon_names[role] = str(canon[0]["name"])
        for role, (fields, body) in parsed.items():
            if self.opencode_dir.is_dir():
                self.check_opencode_agent(role, fields, body, roles, skills)
            if self.copilot_dir.is_dir():
                self.check_copilot_agent(role, fields, body, canon_names)
        if self.opencode_dir.is_dir():
            self.check_opencode_json(roles, canon_tools_by_role)
        if self.copilot_dir.is_dir():
            self.check_repository_settings(canon_tools_by_role)
        self.check_skills(skills)
        self.check_templates()
        self.check_secrets()
        if self.failures:
            for failure in self.failures:
                print(f"FAIL: {failure}", file=sys.stderr)
            print(f"\nparity check failed: {len(self.failures)} problem(s)", file=sys.stderr)
            return 1
        print(
            f"parity OK: {len(roles)} agent(s), {len(skills)} skill(s), "
            f"{self.files_scanned} shipped file(s) scanned"
        )
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="repository root containing agents/, .opencode/, .copilot/ (default: this file's grandparent)",
    )
    args = parser.parse_args()
    return ParityChecker(args.repo_root.resolve()).run()


if __name__ == "__main__":
    raise SystemExit(main())
