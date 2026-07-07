# Agent roles

The platform ships twelve controlled specialist roles. Their canonical definitions are the
markdown manifests in `agents/` — YAML frontmatter declaring tool access and budgets, plus a body
that is the role's instruction set. Each manifest names an output schema; output rules live in
[`agent-output-contracts.md`](../../contracts/agent-output-contracts.md).

## The twelve roles

| Role (manifest) | Purpose | Broker tools | kb_search budget (calls / tokens) |
|---|---|---|---|
| `orchestrator` | Routes work, gathers shared context once, delegates to specialists | `kb_search`, `get_task_context` | 6 / 8,000 |
| `implementation_agent` | Writes the code change | `kb_search`, `get_task_context` | 2 / 3,000 |
| `infra_code_agent` | Writes infrastructure code | `kb_search`, `get_task_context` | 2 / 3,000 |
| `test_layer_agent` | Writes and repairs tests | `kb_search`, `get_task_context` | 1 / 2,000 |
| `delivery_planner_agent` | Sequences delivery | `kb_search` | 1 / 1,200 |
| `pr_planner_agent` | Splits work into PR-sized units | `kb_search` | 1 / 1,200 |
| `adr_writer_agent` | Drafts decision records | `kb_search` | 2 / 3,000 |
| `code_reviewer_agent` | Reviews changes; synthesizes the review panel's findings | `kb_search` | 1 / 2,500 |
| `bug_reviewer_agent` | Review-panel lens: correctness | `kb_search` (via the panel) | 1 / 2,000 |
| `security_reviewer_agent` | Review-panel lens: security | `kb_search` (via the panel) | 1 / 2,000 |
| `quality_reviewer_agent` | Review-panel lens: maintainability | `kb_search` (via the panel) | 1 / 2,000 |
| `test_coverage_reviewer_agent` | Review-panel lens: test coverage | `kb_search` (via the panel) | 1 / 2,000 |

Grant rules:

- **`kb_search` goes to every role.** There is no unrestricted KB search anywhere.
- **`get_task_context` goes only to the task-scoped BUILD-lane roles** — `orchestrator`,
  `implementation_agent`, `infra_code_agent`, `test_layer_agent`. It carries its own server-side
  budget (the 8,000-token Evidence-Pack band), separate from the `kb_search` numbers above.
- Native tools (`read_file`, `read_full`, `list_files`, `grep`, `edit_file`) are granted per role
  and carry no broker budget.
- The four panel reviewers are **not** on the orchestrator's delegation allowlists: they run
  on-demand through the review draft engine from a developer's own session (ADR-0031), never
  in-session. The engine loads their instruction bodies — and `code_reviewer_agent`'s, for the
  synthesis pass — from these manifests at runtime, so editing a manifest changes the panel's
  voice without touching service code.

Budgets are declared in the manifests and **enforced server-side** — a prompt that ignores its
budget still hits the cap in the tool. Editing a manifest or rendering cannot widen access;
identity, ACLs, and allowances bind to the authenticated session on the broker.

## Where the definitions live

| Location | What it is |
|---|---|
| `agents/` | The canon: twelve manifests + `check_parity.py`. |
| `.copilot/` | The GitHub Copilot rendering (repository settings + agent files), hand-authored. |
| `.opencode/` | The OpenCode rendering (`opencode.json` + agent files), hand-authored. |

The renderings are parity-pinned: mcp-server's contract test
(`test_portable_agent_exports.py`) holds them to the checklist in
[`portable-agent-framework.md`](../../contracts/portable-agent-framework.md), and
`python agents/check_parity.py` is the standalone version of the same check for adopters who copy
the three directories without this repo's test suite (exit 0 = parity-clean). Change a manifest
and the tests force the renderings to follow in the same change.

Teams bring their own role descriptions: the pinning is "minimum + whatever exists" — the
framework's roles must stay, and any agent a team adds beside them is held to the same checklist.
