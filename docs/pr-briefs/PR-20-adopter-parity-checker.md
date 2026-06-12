# PR-20 — Adopter-side parity checker + pinned-minimum parity tests

## Why

The framework's promise is "bring your own agents, keep the guarantees" — but the parity
contract tests in `test_portable_agent_exports.py` pin the **exact set** of six roles and three
skills (set-equality on `opencode.json` agents, the skills directory, the repository-settings
union, file counts). The moment an adopting team adds a seventh agent next to ours, the suite
fails — the tests are anti-extensible in exactly the dimension the product sells. And the tests
live inside `services/mcp-server/tests/`, so adopters who copy only `agents/` + `.copilot/` +
`.opencode/` get no parity verification at all.

## Scope

- **`agents/check_parity.py`** — a standalone, stdlib-only (no pyyaml, no pytest), discovery-
  driven parity checker that adopting teams copy along with the trees. It discovers whatever
  roles exist in `agents/*.md` and whatever skills exist in the renderings, and verifies the
  full parity checklist from `docs/contracts/portable-agent-framework.md` for **everything it
  finds**: tool parity (agent files, `opencode.json`, repository-settings union), verbatim
  body + budget lines + framework rules + provenance, composition (deny-by-default permissions,
  handoff/agents consistency, request-discipline tracks the `request_more` grant), host
  validity (modes, skill naming, 30k body cap), and the two-sided secret scan. Exit 0/1 with a
  line-per-failure report — runnable in any adopter's CI.
- **Restructure `test_portable_agent_exports.py`** from exact-set to **pinned-minimum +
  whatever-exists**: the six roles and three skills become `PINNED_*` constants that must exist
  (framework minimum), every *discovered* role/skill — including future team-added ones — must
  pass the same parity checks, and set-equality assertions become superset/membership
  assertions. A smoke test runs `agents/check_parity.py` as a subprocess against this repo and
  asserts exit 0, so the adopter tool itself is CI-verified.
- Contract doc: state the pinned-minimum model and the checker; correct the "generated block"
  wording (the guarantees block is hand-maintained in V1) and record the follow-ups.
- READMEs (`agents/`, `.copilot/`, `.opencode/`) mention the checker; dev-guide updated.
- **Recorded follow-ups** (in the contract doc, not implemented here): (1) skills have no
  canonical source — skill bodies are hand-authored twice (`.opencode/skills/*/SKILL.md` and
  `.copilot/skills/*.md`); promote a canonical `agents/skills/` source. (2) The "Framework
  guarantees" block is hand-maintained; generate renderings from the canon.

## Do NOT

- Do not weaken any individual parity check — only the *set* pinning changes from exact to
  minimum. Tool lists stay exact-match per role.
- Do not add dependencies (pyyaml, click) to the checker or to mcp-server.
- Do not implement the skills-canon or generated-renderings follow-ups.
- Do not touch the broker, schemas, or `MCP_SCHEMA_VERSION` — no runtime change.

## Acceptance criteria

- [ ] `python agents/check_parity.py` exits 0 on this repo and prints a summary.
- [ ] Breaking any parity rule (e.g. a drifted tool list, a missing rendering, a literal-looking
      auth value) makes the checker exit 1 naming the file — covered by tests that run the
      checker against a mutated copy of the trees.
- [ ] Adding a hypothetical extra agent (canon + both renderings + `opencode.json` entry) passes
      both the checker and the restructured pytest suite — covered by a fixture-based test.
- [ ] The six roles + three skills remain a hard minimum: deleting one fails the suite.
- [ ] Contract doc records the pinned-minimum model and both follow-ups.
- [ ] `make verify` green.
