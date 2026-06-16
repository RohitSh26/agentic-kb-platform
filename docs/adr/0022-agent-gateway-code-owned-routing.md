# ADR-0022 — Code-owned intent routing; "broker-governed" vs "broker-assisted"

- Status: Accepted
- Date: 2026-06-16
- Supersedes the implicit assumption in ADR-0009/0021 that the markdown **orchestrator**
  is the universal entry point and that the manifests enforce policy.

## Context

The product agents (orchestrator + 5 specialists) ship as **markdown manifests** rendered for
GitHub Copilot (`.github/agents/`), Copilot CLI/cloud (`.copilot/`), and OpenCode (`.opencode/`),
plus our own runner (`scripts/agent_runner.py`). We told adopters "the orchestrator is your single
entry point."

A plain question — *"Explain how graphify creates code graphs"* — exposed the design:

- The orchestrator treated it as a build task: planned, requested approval, created an Evidence
  Pack, invoked `implementation_agent` (whose `output_schema` is an *implementation plan*), and
  offered to "implement assemble.py." The user never asked to build anything.
- The visible answer was littered with raw evidence UUIDs.
- The **same prompt** under three models (GPT‑5‑mini, Claude Haiku, another) produced **materially
  different behaviour** with the **same manifests**: one over-planned, one used the broker cleanly,
  one skipped the broker entirely.

Root causes: (1) the orchestrator is a *build workflow*, not a router; (2) audit handles (UUIDs)
leak into prose; (3) **a prompt is a suggestion to a probabilistic model, not an enforcement
layer** — so anything load-bearing that lives only in a manifest is non-deterministic.

## Decision

**1. Intent routing and policy are control-plane decisions and live in CODE, in the surfaces we
control.** Routing selects tool allowlist, token budget, evidence policy, verifier profile, human-
approval requirement, output schema, and whether build specialists may run. These must not depend
on a model choosing to obey prose.

We control the loop in exactly two places, and only there can we be **broker-governed**:

- **The MCP broker** (`services/mcp-server`) — sees every tool call; owns tool policy, budgets,
  ACL, the verifier, the ledger, and **code-rendered citations**.
- **Our runner** (`scripts/agent_runner.py`) — we own the whole loop, so it gets the full
  deterministic router + workflow state machine.

**2. Third-party clients are "broker-assisted", not "broker-governed".** In VS Code Copilot,
Codex, and OpenCode the *client's model is the loop*; we cannot insert a deterministic router
between the user and the model. There the manifest is **best-effort prompt routing**, and we MUST
label it honestly. We do not claim full governance for a client whose runtime we do not own. The
broker still enforces what it can server-side (tool allowlist per identity, budgets, ACL, ledger,
verifier, readable citations) — that is the "assist".

**3. The orchestrator is demoted** from universal entry point to the **BUILD/CHANGE** workflow.
The universal entry point is code (the runner's router) where we own the loop; in third-party
clients the orchestrator manifest triages best-effort and routes a question to a read-only
**explainer**, never to the build pipeline.

**4. Two lanes (v1, extensible):**

- `READ_EXPLAIN` — one workflow: `create_pack → expand/open_evidence → synthesize → verify →
  render readable Sources`. Read-only tools, `explanation_answer` schema, no approval, no
  specialists. Ambiguous asks ("how would we fix X?") default to read-only analysis and ask before
  entering BUILD.
- `BUILD_CHANGE` — the existing gated specialist pipeline (implementation → test → review →
  delivery → pr_plan), `phased_pr_plan` schema, approval required.

**5. Differently governed, not lightly governed.** Explanations keep broker-only retrieval,
evidence requirements, ledger, and a verifier pass — they just use an explanation schema and skip
approval/specialists. Governance is intent-specific, not weakened.

**6. Audit handle ≠ citation.** Every evidence card carries two identifiers: `evidence_id` (UUID,
audit/verifier) and `display_citation` (`file:symbol[:lines]`, human) derived **in code** from
artifact metadata. The readable citation is what surfaces; UUIDs never need to appear in a user-
facing answer.

### Code vs prompt responsibility (binding)

| Responsibility | Owner |
|---|---|
| Intent routing / lane selection | **Code** (runner; broker enforces the floor) |
| Tool allowlist · ACL · token budgets · approval gate | **Code** (broker) |
| Output-schema selection · verifier profile · ledger | **Code** |
| Evidence-ID → readable citation rendering | **Code** (broker) |
| Refusing direct file/store access | **Code** (broker tool surface) |
| Human-readable explanation / summary / wording / diagrams | Prompt/model (verified where factual) |
| Drafting an implementation plan *after* the lane is selected | Prompt/model |

## Consequences

- We build a deterministic router + EXPLAIN workflow in the runner, and `display_citation` in the
  broker, before (not instead of) any manifest changes.
- Docs stop claiming "broker-governed" for third-party clients; they say **broker-assisted** and
  point at the runner as the governed reference.
- **Conformance tests** assert transcript-level behaviour per model/client (route, tools used, no
  implementation schema on a question, no raw UUIDs, verifier called) — the only real defence
  against the model variance we observed.
- Known follow-ons (not this ADR): verifier *semantic-support* checking (does evidence back the
  claim, not just exist — see the verifier investigation), retrieval-coverage signals
  ("partial evidence only"), and "one shared pack → governed session object".

## Alternatives rejected

- **"Orchestrator-as-router" purely in the manifest.** Rejected: moves the same non-determinism
  one level deeper; we already observed models ignoring manifest instructions.
- **Weaken governance for explanations.** Rejected: erodes the trust guarantee. Differently
  governed instead.
- **Claim full governance in all clients.** Rejected as dishonest: we do not own the Copilot/Codex/
  OpenCode loop and cannot guarantee the broker invariant there.
