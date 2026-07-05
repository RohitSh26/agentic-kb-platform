# Contract: agent outputs

> Each runtime agent manifest in `agents/` declares an output schema; the MCP
> runtime validates agent outputs against it before accepting them.
> `AGENT_OUTPUT_SCHEMA_VERSION = "1.0.0"`.

## Base rules (all agent outputs)

- Frozen, `extra="forbid"`, versioned: every output carries
  `schema_version: "1.0.0"`.
- **Every claim cites evidence IDs** from the run's Evidence Pack. A claim
  without evidence must be downgraded to an open question — agents never invent
  files, classes, APIs, endpoints, or storage details.
- Outputs are produced by agents that only ever saw broker-mediated evidence;
  they contain no secrets and no direct data-store references.

## Enforcement (two layers)

1. **Construction**: every claim-bearing component (`EvidencedClaim`,
   plan steps, test cases, review findings, rollout steps, planned PRs)
   requires a **non-empty** `evidence_ids` list — an unevidenced claim cannot
   be constructed and must be expressed as an `open_questions` entry instead.
2. **Reference check**: `validate_evidence_references(output, known_evidence_ids)`
   rejects any output citing an `evidence_id` the run's Evidence Pack never
   returned (`AgentOutputValidationError`). Evidence IDs are the broker's
   handles (artifact UUIDs as strings — see `evidence-pack-contract.md`).

## The V1 schemas

Python authority: `services/mcp-server/src/agentic_mcp_server/agent_output_schemas/`
(one module per schema; the `AGENT_OUTPUT_SCHEMAS` registry maps the
`output_schema` name declared in each manifest to its model).

| `output_schema` | Producer manifest | Top-level shape |
|---|---|---|
| `phased_pr_plan_v1` | `agents/orchestrator.md` | `goal`, `phases[]` (name, goal, `changes[]` of evidenced claims, depends_on), `open_questions[]` |
| `implementation_plan_v1` | `agents/implementation.md` | `task`, `steps[]` (description, target_artifacts, `evidence_ids`), `risks[]`, `open_questions[]` |
| `test_plan_v1` | `agents/test_layer.md` | `scope`, `test_cases[]` (name, expectation, `evidence_ids`), `regression_risks[]`, `open_questions[]` |
| `review_findings_v1` | `agents/code_reviewer.md` + the four ADR-0030 review-panel lenses (`agents/bug_reviewer.md`, `agents/security_reviewer.md`, `agents/quality_reviewer.md`, `agents/test_coverage_reviewer.md`) | `verdict` ∈ approve\|request_changes, `findings[]` (severity ∈ blocker\|major\|minor\|note, finding, `evidence_ids`), `open_questions[]` |
| `delivery_plan_v1` | `agents/delivery_planner.md` | `rollout_steps[]` (description, `evidence_ids`), `monitoring[]`, `risks[]`, `open_questions[]` |
| `pr_plan_v1` | `agents/pr_planner.md` | `prs[]` (title, scope, depends_on, `evidence_ids`), `open_questions[]` |
| `adr_draft_v1` | `agents/adr_writer.md` | `title`, `status` (always `proposed` — accepting an ADR is a human act), `context[]` (evidenced claims), `decision`, `consequences[]` (evidenced claims), `alternatives_rejected[]` (alternative, why_rejected, `evidence_ids`), `follow_ups[]`, `open_questions[]` |
| `implementation_plan_v1` (also produced by) | `agents/infra_code.md` | same shape as the `implementation.md` row above — Bicep/Terraform infra changes plan the same way code changes do |

Plans and step lists require at least one entry; finding/risk/monitoring
lists may be empty. `open_questions` is free text by design — it is the only
place an agent may state something without evidence.

## Manifest linkage

Each manifest in `agents/` declares `allowed_tools`. Per ADR-0025 (KB-first,
file-fallback), every one of the twelve manifests grants the budgeted `kb_search`
tool plus native tools (`read_file`, `read_full`, `list_files`, `grep`, `edit_file`
as applicable) — none declares `context.*` or `ledger.*`; those broker tools are
demoted-but-registered (`evidence-pack-contract.md`), not what agent manifests use.
Each manifest also declares `max_context_calls`, `max_context_tokens`,
`requires_evidence_ids: true`, and an `output_schema` key that must exist in
`AGENT_OUTPUT_SCHEMAS`. Manifest budgets must match
`.claude/rules/token-budgets.md`; the broker enforces them server-side
regardless of what the manifest prose says.

## Versioning

Any breaking change bumps `AGENT_OUTPUT_SCHEMA_VERSION`, updates this document
in the same PR, and is validated by mcp-server's contract tests.
