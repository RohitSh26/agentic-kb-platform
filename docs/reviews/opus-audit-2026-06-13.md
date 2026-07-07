# Opus 4.8 repository audit — 2026-06-13

> **Status: DRY RUN — VERIFIED.** This report is the aggregated output of an autonomous, read-only
> audit run by six parallel **Claude Opus 4.8** instances, one per repository partition, commissioned
> by the platform owner, then put through a second **skeptical verification pass** (six more Opus 4.8
> re-checkers + lead review) that re-opened every cited `file:line`. **No GitHub issues have been
> filed yet** — this is the proposed-issue set for owner review. Each finding cites exact `file:line`
> evidence; secret-shaped values are referenced by location, never quoted.
>
> **Verification outcome (of 43 fileable findings): 38 CONFIRMED · 4 REVISED · 1 REFUTED.**
> - REFUTED → dropped: **EV-2** (the "stale comment" is actually correct — it refers to
>   `agent_task_cases/04`+`05`, which match; there is no `case 05` in `retrieval_cases/`).
> - REVISED: **EV-4** severity MEDIUM→LOW (claim-text is not in the recall corpus — test-rigor nit,
>   not a benchmark hole); **EV-3** type `bug`→`documentation` (no runtime defect today; same wrong
>   comment also at `budgets.py:24`); **INF-1** type `invariant-violation`→`tech-debt` (pyright-strict
>   is a `.claude/rules/` deviation, not a numbered invariant) and merged with **MCP-2**; **AG-4**
>   merged into **DOC-7** (same nonexistent-template-canon issue).
> - Net distinct issues to file: **40** (2 high · 8 medium · 30 low).
>
> - Auditor agent: `.claude/agents/opus-repo-auditor.md` (`model: claude-opus-4-8`)
> - Repo commit at audit time: `a823ed1` (main, through PR-21)
> - Rubric: the 7 architecture invariants + V1 exclusion list (CLAUDE.md), the five `.claude/rules/`,
>   the ADRs, and the `docs/contracts/`.

## Executive summary

The platform is in strong shape against its own stated standards. No invariant was found violated in
a way that leaks data or budget, no V1-excluded resource was introduced without an ADR, and no secret
value ships in any file. The security-critical core — fail-closed identity, lock-serialized
check-then-charge budgets, ACL re-filtering at every retrieval surface with no existence oracle,
verbatim untrusted-content handling, the fail-fast allowance parser, and parity-pinned renderings —
all verified clean.

The findings cluster into **four recurring themes**:

1. **Observability is wired but undelivered.** Both services emit structured `event=...` / audit
   `INFO` logs to an **unconfigured logging root**, so in production (default level `WARNING`) those
   lines are silently dropped — directly against the "no silent failures" rule. (kb-builder F2,
   mcp-server F1)
2. **The eval benchmark detects regressions but never fails on them.** The harness computes a
   `regressed` verdict but always exits 0, and non-doc recall is matched by lenient substring
   presence — so the suite that *proves* token savings can let a regression through. (evals F1, F4)
3. **Progress markers and a few contracts have drifted.** The PR-briefs index stops at PR-13, two
   dev-guide "as-of-PR-N" headers disagree, and a copy-pasted "`test` is pointer-only" rationale is
   factually wrong across two files. (docs F1–F5, infra F2)
4. **Hand-duplicated, canon-less artifacts can silently diverge.** The frontmatter parser/constants
   (test ↔ checker), the skill bodies (Copilot ↔ OpenCode), and the template have no body-parity
   gate — the two follow-ups PR-20 already recorded, plus a couple of checker-vs-contract edge
   mismatches. (agents F1–F9)

The one strict-typing rule deviation — mcp-server omits `infrastructure`/`auth` from pyright strict,
contrary to `python.md` — appears from two angles (mcp-server F2, infra F1) and is the clearest
"standard says X, config says Y" gap.

## Severity rollup

_Post-verification counts._

| Severity | Count | Findings |
|---|---|---|
| **High** | 2 | KB-1 (graphify edge duplication), EV-1 (regression gate non-blocking) |
| **Medium** | 8 | KB-2, KB-3 · MCP-1 · MCP-2/INF-1 (merged) · DOC-1, DOC-2 · EV-3 · INF-2 |
| **Low** | 30 | the remainder |
| **Total distinct issues** | **40** | (EV-2 refuted; AG-4 merged into DOC-7; MCP-2+INF-1 one issue; INF-6 informational) |

## Coverage map

| Partition | Scope audited | Findings | Tokens | Tool calls |
|---|---|---|---|---|
| services/kb-builder | connectors, wikify, graphify, linker, indexing, models, migrations | 6 | 72k | 37 |
| services/mcp-server | broker, auth, tool schemas, budgets, evidence, graph, ledger, telemetry | 8 | 119k | 50 |
| agents + .copilot + .opencode | canon manifests, renderings, skills, check_parity.py | 9 | 78k | 38 |
| docs (architecture, adr, contracts, pr-briefs, dev-guide) | contract↔code drift, ADR/brief/doc accuracy | 7 | 107k | 45 |
| evals | harness, cases, metrics, baseline | 8 | 72k | 38 |
| infra + root + .claude | infra, Makefile, compose, CI, settings, build subagents | 6 | 50k | 21 |

---

## Findings

### Partition: services/kb-builder (build plane)

#### KB-1 [HIGH] Graphify edges accrete duplicates on cache-key change (no uniqueness, no stale-cleanup)
- **type**: bug · **severity**: high · **audit-key**: `kb-builder/graphify-edge-duplication`
- **evidence**: `services/kb-builder/src/agentic_kb_builder/infrastructure/postgres/models/knowledge_edge.py:36` — the only unique index is partial to `source='linker'`
- **evidence**: `services/kb-builder/src/agentic_kb_builder/graphify/write.py:100` — `session.add(KnowledgeEdge(... source="graphify" ...))` with no `on_conflict` / reconciliation
- **evidence**: `services/kb-builder/src/agentic_kb_builder/application/build_runner.py:374-392` — `code_graph_cache_key(...)` (versions at 379-380, cache gate at 382): a `GRAPHIFY_VERSION`/`PARSER_CONFIG_VERSION` bump forces a cache miss, re-running `write_code_edges` for an already-edged file
- **why it matters**: Violates the connectors rule "build jobs must be safely re-runnable (no duplicate artifacts/edges/cache rows on retry)." Unlike linker edges (which `_delete_stale` + upsert), graphify edges have neither a unique constraint nor a stale sweep — a version bump or content change leaves prior `source='graphify'` edges and inserts new ones, so graph queries see duplicate `calls`/`imports` edges.
- **suggested fix**: Add a partial unique index `(from_artifact_id, to_artifact_id, edge_type) WHERE source='graphify'` and switch to `on_conflict_do_update`; or add graphify stale-edge reconciliation analogous to `_delete_stale`. Add a regression test (graphify → bump version → rebuild → assert edge count unchanged).

#### KB-2 [MEDIUM] Build/retrieval info logs silently dropped by default (no level set)
- **type**: code-improvement · **severity**: medium · **audit-key**: `kb-builder/info-logs-suppressed`
- **evidence**: `services/kb-builder/src/agentic_kb_builder/structured_logging.py:10-17` — `get_logger` adds a `StreamHandler` but never `setLevel`; no `basicConfig`/`setLevel(INFO)` anywhere
- **evidence**: `services/kb-builder/src/agentic_kb_builder/application/build_runner.py:154` — `logger.info("event=build_run_started ...")` (and ~20 more `event=` info logs)
- **why it matters**: `python.md`/CLAUDE.md require "structured logs on every build and retrieval path. No silent failures." With Python's default root level `WARNING`, every `logger.info(event=...)` is suppressed at runtime — exactly the build observability the rule mandates.
- **suggested fix**: Set the logger/handler level to `INFO` in `get_logger` (or configure logging at the build entrypoint honoring `LOG_LEVEL`); consider `logger.propagate = False` to avoid double emission.

#### KB-3 [MEDIUM] Index orphan-sweep and consistency check load the entire registry body text
- **type**: performance · **severity**: medium · **audit-key**: `kb-builder/full-registry-projection-load`
- **evidence**: `services/kb-builder/src/agentic_kb_builder/indexing/upsert.py:76` — `expected = {doc.doc_id for doc in await load_search_docs(session)}` (full registry, only doc_ids used)
- **evidence**: `services/kb-builder/src/agentic_kb_builder/indexing/consistency.py:25` — full registry loaded for only `doc_id` + `artifact_hash`
- **evidence**: `services/kb-builder/src/agentic_kb_builder/indexing/projection.py:37-49,53` — `load_search_docs` selects full `KnowledgeArtifact` objects (incl. `body_text`) and joins embedding vectors for every artifact (the cited `:55` is the per-artifact embedding lookup)
- **why it matters**: Both run every build and need only `(doc_id, artifact_hash)`, yet pull every artifact's full body plus all vectors into memory — an unbounded fetch with needless heavy columns that grows with the whole KB nightly.
- **suggested fix**: Add a light projection (`load_doc_hashes(session) -> dict[doc_id, artifact_hash]`) for orphan-sweep and consistency; reserve `load_search_docs` for the upsert path that needs bodies/vectors.

#### KB-4 [LOW] Linker stale-edge load is unbounded across all kb_versions
- **type**: performance · **severity**: low · **audit-key**: `kb-builder/linker-unbounded-load`
- **evidence**: `services/kb-builder/src/agentic_kb_builder/linker/write_edges.py:100-107` — loads every linker edge (no kb_version/pagination bound) to diff in Python
- **evidence**: `services/kb-builder/src/agentic_kb_builder/linker/run.py:51-63` — `_load_linkable_artifacts` selects all non-deleted artifacts (incl. `body_text`) into memory each run
- **why it matters**: Nightly linker reconciliation is O(all edges)+O(all artifacts) in memory; fine at V1 scale but an unbounded fetch that grows with the registry.
- **suggested fix**: Consider a server-side `DELETE ... WHERE NOT EXISTS` against a temp table of computed keys, and stream/paginate artifact loads as the KB grows; acceptable to defer — record the bound.

#### KB-5 [LOW] Derived artifacts never inherit `source_item.acl_teams` (documented follow-up, untracked by tests)
- **type**: test-gap · **severity**: low · **audit-key**: `kb-builder/acl-propagation-pending`
- **evidence**: `services/kb-builder/src/agentic_kb_builder/wikify/write.py:30-41` and `graphify/write.py:45-57` — `KnowledgeArtifact(...)` built without `acl_teams`
- **evidence**: `docs/contracts/postgres-knowledge-registry.md:56-59` — documents propagation as a kb-builder follow-up
- **why it matters**: Contract-acknowledged (not a silent bug), but every derived artifact stays org-public, making the broker's `team_acl_v1` filter a no-op for restricted sources. No test pins the current org-public behavior, so the follow-up can be forgotten and a non-public source would leak once any ACL exists.
- **suggested fix**: When implementing propagation, set `acl_teams=list(fetched.source.acl_teams)` in both writers; until then add a test pinning the org-public default + a TODO referencing the contract line.

#### KB-6 [LOW] `health()` is a stale stub though its prerequisite (build engine) has landed
- **type**: tech-debt · **severity**: low · **audit-key**: `kb-builder/health-stub-stale`
- **evidence**: `services/kb-builder/src/agentic_kb_builder/health.py:1` — "Static health stub; real build-run reporting arrives with the build engine (PR-04)."
- **evidence**: `services/kb-builder/src/agentic_kb_builder/health.py:4-5` — returns a constant `{"status":"ok"}`
- **why it matters**: The build engine has landed, so the stub's stated precondition is met, yet health never reports last build status/kb_version.
- **suggested fix**: Report last `kb_build_run` status/kb_version/completed_at, or update the comment to the real follow-up if deferral is intentional.

### Partition: services/mcp-server (runtime plane / Context Broker)

#### MCP-1 [MEDIUM] Broker and audit INFO logs emitted to an unconfigured logging root, dropped in production
- **type**: test-gap · **severity**: medium · **audit-key**: `mcp-server/unconfigured-logging-root`
- **evidence**: `services/mcp-server/src/agentic_mcp_server/__main__.py:6` — `create_app().run(...)` with no `logging.basicConfig`/handler setup
- **evidence**: `services/mcp-server/src/agentic_mcp_server/context_broker/request_more.py:36` — plain `logging.getLogger(__name__)` (same in `pack.py:43`, `evidence.py:31`, `graph.py:37`, `ledger.py:24`)
- **evidence**: `services/mcp-server/src/agentic_mcp_server/telemetry/audit.py:16` — the security audit stream, also via plain getLogger
- **why it matters**: `python.md` + the contract require audit logging of every expansion/source access to structured stdout. Default root level `WARNING` + no handler means every broker/audit `INFO` line (including ACL-suppression and injection-detection records) is discarded. Tests pass only because pytest installs a root handler at INFO.
- **suggested fix**: Configure logging at boot in `create_app()`/`main()` using the `structured_logging` formatter, or route broker/audit modules through `get_logger`. Add a test asserting an audit line emits under default (non-caplog) config.

#### MCP-2 / INF-1 [MEDIUM] pyright strict omits `infrastructure` (and `auth`), contrary to python.md — ONE issue
- **type**: tech-debt _(INF-1 was mislabeled `invariant-violation`; verification retyped to tech-debt — strict-pyright is a `.claude/rules/` deviation, not one of the 7 numbered invariants)_ · **severity**: medium · **audit-key**: `mcp-server/strict-missing-infrastructure`
- **evidence**: `services/mcp-server/pyproject.toml:47-51` — strict list is `tool_schemas`, `agent_output_schemas`, `domain` only; `services/kb-builder/pyproject.toml:45` includes both `domain` and `infrastructure` (the asymmetry)
- **why it matters**: `python.md` requires pyright strict on each service's domain, **infrastructure**, and tool-schema packages. `infrastructure/` (Postgres access, the `SearchClient` seam) escapes strict. (Verification note: the rule mandates `infrastructure`; `auth` is an auditor recommendation, not a rule mandate.)
- **suggested fix**: Add `src/agentic_mcp_server/infrastructure` (and ideally `auth`) to the strict list and resolve resulting `reportUnknownMemberType` on SQLAlchemy `Row` access.

#### MCP-3 [LOW] `/health` reports a `service` name that disagrees with the configured server name
- **type**: documentation · **severity**: low · **audit-key**: `mcp-server/health-service-name-mismatch`
- **evidence**: `services/mcp-server/src/agentic_mcp_server/health.py:34,39` — `"service": "mcp-server"`
- **evidence**: `services/mcp-server/src/agentic_mcp_server/config.py:11` — `SERVER_NAME = "agentic-kb-context-broker"`
- **why it matters**: The one public unauthenticated route names the service inconsistently with the name advertised over MCP.
- **suggested fix**: Return `SERVER_NAME` from config in both health branches.

#### MCP-4 [LOW] `open_evidence` ledger row logs the artifact under `reused_evidence_ids` even on a charged expansion
- **type**: code-improvement · **severity**: low · **audit-key**: `mcp-server/open-evidence-reused-column`
- **evidence**: `services/mcp-server/src/agentic_mcp_server/context_broker/evidence.py:98-99` — `returned_artifact_ids=[card.artifact_id], reused_evidence_ids=[card.artifact_id]`
- **why it matters**: An `approved` open_evidence charges tokens yet logs the id only in the `reused` column with `new_evidence_ids` empty, so ledger analytics that distinguish reuse from fresh access miscount expansions as reuse. (`tokens_returned` is correct — labeling issue, not a budget leak.)
- **suggested fix**: Record the expanded artifact under `new_evidence_ids` when `status=="approved"`, or document the open_evidence convention in the contract.

#### MCP-5 [LOW] `_card_summary` indexes `splitlines()[0]` after an independent truthiness guard
- **type**: code-improvement · **severity**: low · **audit-key**: `mcp-server/card-summary-splitlines`
- **evidence**: `services/mcp-server/src/agentic_mcp_server/context_broker/retrieval.py:53-55` — `first_line = body.strip().splitlines()[0] if body.strip() else ""` (computes `body.strip()` repeatedly; guard and index evaluated independently)
- **why it matters**: Works today, but fragile on the untrusted-content path.
- **suggested fix**: `stripped = body.strip(); first = next(iter(stripped.splitlines()), "")`.

#### MCP-6 [LOW] `fetch_artifacts` inner-joins `source_item`, so an orphaned artifact silently disappears from every retrieval surface
- **type**: tech-debt · **severity**: low · **audit-key**: `mcp-server/artifacts-inner-join-source`
- **evidence**: `services/mcp-server/src/agentic_mcp_server/infrastructure/postgres/artifacts.py:20-25` — `FROM knowledge_artifact a JOIN source_item s ON s.source_id = a.source_id`
- **why it matters**: This INNER JOIN is the single hydration path for cards/reads/reuse/open_evidence/graph. Safe under the stated schema, but a build-plane anomaly (orphaned/source-deleted artifact) would make evidence vanish with no diagnostic — a silent-failure risk on the read path.
- **suggested fix**: Keep the INNER JOIN but warn when fewer rows return than ids requested, or LEFT JOIN with nullable `source_uri` so a sourceless artifact is still diagnosable.

#### MCP-7 [LOW] No test asserts `denied` (per-agent) wins over `needs_human_approval` (per-run)
- **type**: test-gap · **severity**: low · **audit-key**: `mcp-server/request-more-precedence-untested`
- **evidence**: `services/mcp-server/src/agentic_mcp_server/context_broker/request_more.py:122-141` — per-agent denial precedes the run-budget `needs_human_approval` check
- **evidence**: `services/mcp-server/tests/integration/test_context_broker.py:441-461` — the escalation test uses a generous policy, so the per-agent gate never fires; no test forces both conditions
- **why it matters**: The contract fixes the order `reused → approved → denied → needs_human_approval`; a future reordering of the two `if` blocks would pass CI.
- **suggested fix**: Add a case where the per-agent allowance is exhausted AND the run budget is too small, asserting `status=="denied"`.

#### MCP-8 [LOW] Unused `TokenBudget` domain class is dead code on the broker path
- **type**: tech-debt · **severity**: low · **audit-key**: `mcp-server/tokenbudget-dead-code`
- **evidence**: `services/mcp-server/src/agentic_mcp_server/domain/token_budget.py:15-27` — `class TokenBudget` with `remaining_tokens`/`can_spend`
- **evidence**: enforcement actually lives in `context_broker/state.py:38-47` + `budgets.py`; only `CHARS_PER_TOKEN`/`estimate_tokens` are imported from this module
- **why it matters**: A parallel, untested budget abstraction wired into no enforcement path invites drift (someone could "fix budgets" here and change nothing).
- **suggested fix**: Remove `TokenBudget` (keep `CHARS_PER_TOKEN`/`estimate_tokens`), or refactor `EvidencePackState`/`AgentUsage` to use it. Confirm no test references it first.

### Partition: agents + .copilot + .opencode (portable agent framework)

#### AG-1 [LOW] OpenCode `tools` value-enablement check is in an `elif`, masked by set-drift
- **type**: tech-debt · **severity**: low · **audit-key**: `agents/opencode-tools-elif-masks-value-check`
- **evidence**: `agents/check_parity.py:268` / `:270` — the `value != "true"` check is an `elif` of the set-drift check
- **why it matters**: When the tool key set drifts, the checker never validates that surviving entries are `true` — partially defeating the failure-collecting intent for that field.
- **suggested fix**: Split into two independent `if` checks so both problems are collected in one run.

#### AG-2 [LOW] Checker forces Copilot `handoffs` whenever `agents` is declared — stricter than the contract
- **type**: tech-debt · **severity**: low · **audit-key**: `agents/copilot-handoffs-over-required`
- **evidence**: `agents/check_parity.py:354-357` — requires handoffs to mirror `agents`
- **evidence**: `docs/contracts/portable-agent-framework.md:99-100` — "`handoffs` is VS Code-only (the cloud agent ignores it)."
- **why it matters**: A cloud-only orchestrator legitimately omitting `handoffs` is rejected — a checker/contract mismatch on an optional field.
- **suggested fix**: Validate `handoffs` shape only when present; don't require it solely because `agents` is non-empty.

#### AG-3 [LOW] Malformed known MCP config double-reports (unreadable JSON + "no Authorization reference")
- **type**: code-improvement · **severity**: low · **audit-key**: `agents/secret-scan-double-report`
- **evidence**: `agents/check_parity.py:558-560` and `:567-571` — re-parses an already-failed file and adds a confusing second failure
- **why it matters**: One bad file yields up to three failure lines, making adopter output noisier than necessary (no crash).
- **suggested fix**: Cache the parsed config or short-circuit the two-sided check when the file already failed to parse.

#### AG-4 [MERGED into DOC-7 — file once] `agents/_template.md` canon referenced by the contract does not exist
- **verdict**: **REVISE/merge.** Real (no `agents/_template.md` exists; templates are render-only), but
  it is the same defect as DOC-7. Verification note: the strongest cite is contract `:119` (the bare
  `_template.md`); ADR `:34` actually scopes `_template` to the rendered trees (so it does *not* imply
  canon), and contract `:11` is about manifest discovery, not templates. **File once, as DOC-7.**

#### AG-5 [LOW] Copilot template body length never checked against the 30k cap
- **type**: test-gap · **severity**: low · **audit-key**: `agents/template-body-cap-unchecked`
- **evidence**: `agents/check_parity.py:366-367` — cap enforced only per discovered role; `:497-523` `check_templates` omits the length check
- **why it matters**: The 30k cap is a host-validity rule (not a per-manifest body-parity item), so a bloated template would ship invalid yet pass the checker.
- **suggested fix**: Add `len(body) < COPILOT_MAX_BODY_CHARS` for the copilot template in `check_templates`.

#### AG-6 [LOW] `.copilot/README.md` discovery-location prose drifts from the ADR
- **type**: documentation · **severity**: low · **audit-key**: `agents/copilot-readme-discovery-path`
- **evidence**: `.copilot/README.md:18` labels `~/.copilot/agents/` "for a VS Code user profile"
- **evidence**: `docs/adr/0009-portable-agent-framework.md:12,37` — VS Code uses `.github/agents/`; `~/.copilot/agents/` is the Copilot CLI profile
- **why it matters**: Could send an adopter to the wrong directory; README prose isn't CI-pinned.
- **suggested fix**: Clarify the host mapping to match the ADR.

#### AG-7 [LOW] Duplicated frontmatter parser + constants across checker and contract test
- **type**: duplicate-code · **severity**: low · **audit-key**: `agents/parser-constants-duplicated`
- **evidence**: `agents/check_parity.py:41-52` / `:62-147` vs `services/mcp-server/tests/contract/test_portable_agent_exports.py:44-56` / `:63-146` — byte-for-byte the same
- **why it matters**: Accepted (ADR-0008 forbids the test importing the root checker), but a parser fix in one can silently diverge from the other.
- **suggested fix**: Keep duplicated; add a cross-check test asserting the constant sets (`AUTH_REFERENCE_RE`/`REQUEST_MORE_FIELDS`/`SECRET_MARKERS`) are identical (comparing literals doesn't cross the boundary).

#### AG-8 [LOW] `discovered_skills` unions both render trees; the contract test discovers from OpenCode only
- **type**: tech-debt · **severity**: low · **audit-key**: `agents/skill-discovery-union-vs-opencode`
- **evidence**: `agents/check_parity.py:188-196` (union) vs `services/mcp-server/tests/contract/test_portable_agent_exports.py:178-179` (OpenCode only)
- **why it matters**: The per-agent skill membership check uses the union, so an adopter allow-listing a skill present only in `.copilot/` could pass the checker while failing the repo suite.
- **suggested fix**: Anchor the checker's per-agent skill set to OpenCode (or assert equality before using the union).

#### AG-9 [LOW] Skill bodies hand-duplicated between hosts with no canon and no body-parity check
- **type**: duplicate-code · **severity**: low · **audit-key**: `agents/skill-body-no-parity`
- **evidence**: `.opencode/skills/evidence-citation/SKILL.md:18-26` vs `.copilot/skills/evidence-citation.md:18-22` — identical safety prose, two files
- **evidence**: `docs/contracts/portable-agent-framework.md:138-141` — recorded follow-up #1
- **why it matters**: The two renderings of each skill (untrusted-content / request-more rules) can silently diverge — exactly the safety prose the framework relies on — with no gate.
- **suggested fix**: Promote a canonical `agents/skills/` source (the recorded follow-up); interim, add a body-equality assertion between host skill files.

### Partition: docs (architecture, ADRs, contracts, briefs, dev-guide)

#### DOC-1 [MEDIUM] Contracts and code call the `test` artifact "pointer-only" but it carries a body
- **type**: documentation · **severity**: medium · **audit-key**: `docs/test-pointer-only-wrong`
- **evidence**: `services/kb-builder/src/agentic_kb_builder/indexing/search_document.py:11-13` — "Pointer-only artifacts (code_file, endpoint, test) ... have no body to rank"
- **evidence**: `services/kb-builder/src/agentic_kb_builder/graphify/to_artifacts.py:36-46` — `test` drafts are built with `body_text=_snippet(...)` and a span
- **evidence**: `docs/contracts/azure-ai-search-index.md:41` repeats the wrong rationale; `docs/dev-guide/02-implementation-tour.md:206-208` (now `docs/dev-guide/21-code-tour.md`) states the correct behavior (contradiction)
- **why it matters**: A load-bearing shared contract states a false rationale — `test` rows DO have a body but are deliberately excluded from the projection. A reader building L2 retrieval over tests via graph edges would be misled.
- **suggested fix**: Reword the docstring + contract to: "`test` and `code_symbol` carry snippet bodies; `test` is excluded from the Search projection (reachable via graph edges); `code_file`/`endpoint` are genuinely pointer-only (`body_text=None`)."

#### DOC-2 [MEDIUM] PR-briefs index stops at PR-13 but PR-14..PR-20 shipped
- **type**: documentation · **severity**: medium · **audit-key**: `docs/pr-briefs-index-stale`
- **evidence**: `docs/pr-briefs/README.md:7-21` lists only PR-01..PR-13
- **evidence**: `docs/pr-briefs/` contains PR-14..PR-20 briefs
- **why it matters**: The README is the stated build queue and the `/next-pr` entry point; omitting half the shipped briefs misrepresents project state.
- **suggested fix**: Extend the table through PR-20.

#### DOC-3 [LOW] Dev-guide "as-of" headers disagree with each other and with shipped scope
- **type**: documentation · **severity**: low · **audit-key**: `docs/dev-guide-as-of-drift`
- **evidence**: `docs/dev-guide/02-implementation-tour.md:1` (now `docs/dev-guide/21-code-tour.md`) — "(PR-01 → PR-15)" while body documents through PR-20
- **evidence**: `docs/dev-guide/README.md:9` — "(PR-01 → PR-16)"
- **why it matters**: Three different "current as of" markers make all of them untrustworthy.
- **suggested fix**: Set both to "PR-01 → PR-21" (or drop the range and say "current main").

#### DOC-4 [LOW] Implementation-tour migration list omits 0007 and 0008
- **type**: documentation · **severity**: low · **audit-key**: `docs/dev-guide-migrations-incomplete`
- **evidence**: `docs/dev-guide/02-implementation-tour.md:87-91` (now `docs/dev-guide/21-code-tour.md`) — enumerates 0001..0006 and stops
- **evidence**: `services/kb-builder/migrations/versions/0007_retrieval_event_status.py`, `0008_acl_teams.py` exist
- **why it matters**: The omitted migrations back two heavily-documented features (ledger status, team ACLs).
- **suggested fix**: Append 0007 (ledger status) and 0008 (acl_teams) to the walk.

#### DOC-5 [LOW] Architecture `retrieval_event` schema sketch omits the load-bearing `status` column
- **type**: documentation · **severity**: low · **audit-key**: `docs/arch-retrieval-event-status-missing`
- **evidence**: `docs/architecture/00-overview.md:95-98` — sketch has no `status`
- **evidence**: `services/kb-builder/src/agentic_kb_builder/infrastructure/postgres/models/retrieval_event.py:23` — `status` NOT NULL default `'approved'`
- **why it matters**: `status` is the column the whole `ledger.list_retrievals` contract hinges on; the canonical sketch predates it.
- **suggested fix**: Add `status` to the sketch (already labeled a "sketch," so a one-line add keeps it honest).

#### DOC-6 [LOW] Portable-agent namespace table covers 5 of 6 broker tools without noting `graph.get_neighbors`
- **type**: documentation · **severity**: low · **audit-key**: `docs/namespace-table-missing-graph`
- **evidence**: `docs/contracts/portable-agent-framework.md:35-41` — table omits `graph.get_neighbors`
- **evidence**: `services/mcp-server/src/agentic_mcp_server/mcp/tool_registry.py:34-41` — six tools incl. `graph.get_neighbors`, granted to no agent
- **why it matters**: A team-added agent wanting graph traversal has no mapping row, and the omission is silent.
- **suggested fix**: Add a note: "`graph.get_neighbors` maps the same way but is granted to no framework agent in V1."

#### DOC-7 [LOW] Composition table phrase "the five specialists + template" implies a template manifest that doesn't exist
- **type**: documentation · **severity**: low · **audit-key**: `docs/composition-table-template`
- **evidence**: `docs/contracts/portable-agent-framework.md:88-91` — lumps "template" into the specialist grant row
- **evidence**: `agents/` has no `_template.md`; `check_parity.py:470-523` validates only the rendering templates
- **why it matters**: Reads as if a canonical template manifest is part of the pinned minimum. **Absorbs AG-4** (same defect — the contract's bare `_template.md` at `:119` is the ambiguous line; ADR `:34` already scopes templates to the rendered trees).
- **suggested fix**: Footnote the row + disambiguate `:119`: "template = the rendering skeletons `_template.*` only; there is no `agents/_template.md` canon."

### Partition: evals (evaluation harness)

#### EV-1 [HIGH] Baseline "regressed" verdict never fails the run — token regressions slip through
- **type**: bug · **severity**: high · **audit-key**: `evals/regression-verdict-non-blocking`
- **evidence**: `evals/run.py:126-130` — exits 1 only on `not record.succeeded`; never branches on `comparison.verdict`
- **evidence**: `evals/harness/baseline.py:76-78` — computes `verdict = "regressed"` but nothing consumes it as an exit condition
- **why it matters**: This benchmark is how the project *proves* the broker saves tokens (invariant 3; "budgets are asserted in tests"). A real efficiency regression yields verdict `regressed` in the table but exit code 0, so any CI keying on exit status passes a regression.
- **suggested fix**: Add `--fail-on-regress` (default on) so `main()` returns nonzero when `verdict=="regressed"`; have the eval-runner assert on it; document the gating in `docs/contracts/evals-report.md`.

#### EV-2 [REFUTED — DROPPED, not filed] "Stale" inline doc on the reuse-coverage assertion
- **verdict**: **REFUTED by verification.** The comment at `evals/tests/test_harness_end_to_end.py:66`
  ("case 04 scripts an exact reuse and case 05 a semantic reuse") is **correct**: it refers to
  `agent_task_cases/04_tests_for_similar_endpoint.yaml` (two identical questions → exact reuse) and
  `agent_task_cases/05_release_monitoring_guidance.yaml` (reworded question, cosine ≈ 0.935 ≥ 0.90 →
  semantic reuse). There is no `case 05` in `retrieval_cases/` (it holds only 01–04), so the comment
  cannot refer to that directory. The original premise was a misread. **Not filed.**

#### EV-3 [MEDIUM] `cases.py` (and `budgets.py`) comment misstates the fallback allowance
- **type**: documentation _(verification retyped from `bug` — no runtime defect today)_ · **severity**: medium · **audit-key**: `evals/wrong-default-allowance-comment`
- **evidence**: `evals/harness/cases.py:40-41` — "fall back silently to the broker's smallest default allowance"
- **evidence**: `services/mcp-server/.../context_broker/budgets.py:25` — default is `1/2500`; `evals/harness/executor.py:60-61` — delivery/pr-planner are `1/1500` (smaller)
- **evidence (added by verification)**: `services/mcp-server/.../context_broker/budgets.py:24` — carries the *same* wrong comment ("the smallest role allowance (delivery planner)") though delivery planner is 1500, not 2500
- **why it matters**: The default (2500) is NOT the smallest, so a subject typo falls through to a *larger* allowance than intended for those agents. No committed case currently relies on a 1500 denial for those subjects (see EV-8), so this is a misleading-comment defect, not a live bug — but it masks a real latent risk.
- **suggested fix**: Fix both comments; assert `DEFAULT_AGENT_ALLOWANCE` is never larger than any relied-on manifest allowance, or have the executor reject unknown subjects rather than defaulting.

#### EV-4 [LOW] File/symbol/test recall uses lenient case-folded substring matching
- **type**: test-gap · **severity**: low _(verification downgraded from medium; claim-text rationale struck)_ · **audit-key**: `evals/lenient-substring-recall`
- **evidence**: `evals/harness/executor.py:241-247` — `item.casefold() not in folded_corpus` over the broker-returned corpus
- **why it matters**: `missing_context_rate` treats a symbol as "returned" if its string is a substring of returned card/expansion text, so an unrelated substring could false-positive. **Correction from verification**: the recall corpus (`corpus_parts`) is built only from broker output (pack summary, card title/summary, `request_more` cards, `open_evidence` text) — scripted claim text is NOT in it, so recall *is* grounded in broker output; the original "matches scripted claims" rationale was wrong. This is a test-rigor nit, not a benchmark-integrity hole.
- **suggested fix**: Anchor non-doc expectations to specific returned cards (require the symbol in a named returned card's text) rather than any-substring presence.

#### EV-5 [LOW] `semantic_cache_hit_rate` denominator includes denied/needs_human_approval rows
- **type**: tech-debt · **severity**: low · **audit-key**: `evals/semantic-rate-denominator`
- **evidence**: `evals/harness/metrics.py:77,89-91` — denominator is all `context.request_more` rows
- **why it matters**: A denied follow-up never could be a semantic reuse, yet counts against the rate — cases that script denials drag the headline metric down (which feeds the EV-1 verdict). Internally consistent with the contract, but a measurement-honesty smell.
- **suggested fix**: Decide/document whether the denominator should be charged follow-ups only; if changed, update `metrics.py` + `docs/contracts/evals-report.md` and regenerate `baseline.json`.

#### EV-6 [LOW] FakeSearchClient ordering is unstable on equal scores
- **type**: tech-debt · **severity**: low · **audit-key**: `evals/fake-search-tie-order`
- **evidence**: `services/mcp-server/.../infrastructure/search/search_client.py:47` — `sorted(..., key=lambda hit: hit.score, reverse=True)` (no tiebreaker)
- **evidence**: `evals/harness/fixtures.py:92-95` — cross-seed score collisions possible
- **why it matters**: Latent today (broker reranks, cases assert sets), but an order-sensitive future metric could flake. (Note: the code lives in mcp-server — cross-boundary.)
- **suggested fix**: Add a stable secondary key `(-score, str(artifact_id))`, or document that tie order is not contractual.

#### EV-7 [LOW] Agent-subject naming drift between harness and the budget rule is undocumented
- **type**: documentation · **severity**: low · **audit-key**: `evals/budget-naming-drift`
- **evidence**: `evals/harness/executor.py:54-62` — keys `impl-agent`/`test-agent`/... at the top of each rule range, uncommented
- **evidence**: `.claude/rules/token-budgets.md` — names allowances but not the canonical subject strings
- **why it matters**: A reader can't verify harness numbers against the rule without guessing the name mapping; a future budget tune won't be caught by any test.
- **suggested fix**: Comment each subject→rule-line mapping and/or add a test that harness allowances stay within documented ranges; record canonical subject strings.

#### EV-8 [LOW] No committed case exercises an actual budget denial (only a synthetic unit test does)
- **type**: test-gap · **severity**: low · **audit-key**: `evals/no-denial-benchmark-case`
- **evidence**: `evals/agent_task_cases/01_plan_new_endpoint.yaml:30-41` — stays within allowance; only `test_harness_end_to_end.py:114-135` (synthetic) asserts denial
- **why it matters**: The suite producing `baseline.json` never drives a `denied`/`needs_human_approval` path, so per-agent/per-run budget enforcement is unrepresented in the committed report.
- **suggested fix**: Add one agent-task case that scripts an over-allowance request and asserts the denial ledger status.

### Partition: infra + root + .claude

#### INF-1 [MERGED with MCP-2 — file once] mcp-server pyright strict omits `infrastructure`
- **verdict**: **REVISE/merge.** Confirmed real, but it is the *same* gap as MCP-2 and its type was wrong
  (`invariant-violation` → `tech-debt`). See the merged **MCP-2 / INF-1** entry above. **File once.**

#### INF-2 [MEDIUM] `.mcp.json` references the pre-restructure `apps/` path and wrong port
- **type**: documentation · **severity**: medium · **audit-key**: `infra/mcp-json-apps-path-and-port-drift`
- **evidence**: `.mcp.json:21` — "Once **apps/mcp-server** is running..."; `:23` — `"url": "http://localhost:8080/mcp"`
- **evidence**: `docker-compose.yml:56` + `services/mcp-server/Dockerfile` — port 8000
- **why it matters**: ADR-0008 moved everything under `services/`; `apps/` no longer exists, and the dogfooding URL points at a dead port/path.
- **suggested fix**: Update the comment to `services/mcp-server` and the URL to port `8000` (confirm the path).

#### INF-3 [LOW] `settings.json` migration deny glob never matches real migration filenames
- **type**: tech-debt · **severity**: low · **audit-key**: `infra/settings-dead-applied-migration-deny`
- **evidence**: `.claude/settings.json:47` — `Edit(./services/kb-builder/migrations/versions/*_applied_*.py)`
- **evidence**: actual files are `0001_...`..`0008_...` (no `_applied_` token)
- **why it matters**: The guard meant to protect applied migrations matches nothing — a false sense of safety.
- **suggested fix**: Adopt the `_applied_` convention, or rewrite the deny to match reality, or drop it.

#### INF-4 [LOW] CI uses the shared `postgres` database for tests, no per-job isolation
- **type**: test-gap · **severity**: low · **audit-key**: `infra/ci-uses-default-postgres-db`
- **evidence**: `.github/workflows/ci.yml:27,59,101` — `.../postgres` (maintenance DB) for all three jobs
- **evidence**: `Makefile:2` + `.env.example` — local convention is a dedicated `agentic_kb_test` DB
- **why it matters**: Works today (separate runners), but diverges from the documented convention and is fragile to residue.
- **suggested fix**: Create/target a dedicated `agentic_kb_test` DB in CI to mirror the Makefile/.env convention.

#### INF-5 [LOW] `migration-writer` build subagent uses bare `alembic`, not `uv run alembic`
- **type**: documentation · **severity**: low · **audit-key**: `infra/migration-writer-bare-alembic`
- **evidence**: `.claude/agents/migration-writer.md` — instructs `alembic upgrade head` / `downgrade -1`
- **evidence**: `Makefile:28` + CI use `uv run alembic`; bare `alembic` isn't on PATH in a uv project without activation
- **why it matters**: The one agent that authors schema changes has verification commands that would fail outside an activated venv.
- **suggested fix**: Use `uv run alembic ...` in migration-writer.md.

#### INF-6 [INFORMATIONAL — no action] infra/ provisions nothing yet; footprint description is accurate
- **type**: (none) · **severity**: informational · **audit-key**: `infra/readme-footprint-informational`
- **evidence**: `infra/README.md:3-12` — App Insights / Managed Identity + Key Vault are not on the ADR-0007 exclusion list; "No IaC is authored yet"
- **why it matters**: Confirms the infra partition introduces no V1-excluded resource. The managed-identity matrix correctly denies the mcp-server identity Search/OpenAI access (invariant 6).
- **suggested fix**: None now; when IaC is authored, match this matrix and reference ADR-0006/0007. **Not proposed as an issue.**

---

## Proposed GitHub issues (for owner review — NOT yet filed)

Labels to be created first: `code-improvement`, `security`, `test-gap`, `tech-debt`, `performance`,
`architecture`, `invariant-violation`, `severity:high`, `severity:medium`, `severity:low`, `opus-audit`.
(`bug`, `enhancement`, `documentation`, `duplicate` already exist.) Every issue carries `opus-audit`
plus its type and severity labels, and an `audit-key` line in the body for idempotent re-runs.

| # | Title | Type label | Severity |
|---|---|---|---|
| KB-1 | [kb-builder] Graphify edges accrete duplicates on cache-key change | bug | high |
| KB-2 | [kb-builder] Build/retrieval INFO logs silently dropped (no log level set) | code-improvement | medium |
| KB-3 | [kb-builder] Orphan-sweep & consistency check load full registry body text | performance | medium |
| KB-4 | [kb-builder] Linker stale-edge load is unbounded across kb_versions | performance | low |
| KB-5 | [kb-builder] Derived artifacts never inherit source_item.acl_teams (untested) | test-gap | low |
| KB-6 | [kb-builder] health() is a stale stub though the build engine has landed | tech-debt | low |
| MCP-1 | [mcp-server] Broker/audit INFO logs dropped via unconfigured logging root | test-gap | medium |
| MCP-2/INF-1 | [mcp-server] pyright strict omits infrastructure (python.md) — merged | tech-debt | medium |
| MCP-3 | [mcp-server] /health service name disagrees with configured SERVER_NAME | documentation | low |
| MCP-4 | [mcp-server] open_evidence logs charged expansion under reused_evidence_ids | code-improvement | low |
| MCP-5 | [mcp-server] _card_summary splitlines guard is redundant/fragile | code-improvement | low |
| MCP-6 | [mcp-server] fetch_artifacts INNER JOIN hides orphaned artifacts silently | tech-debt | low |
| MCP-7 | [mcp-server] No test pins denied-over-needs_human_approval precedence | test-gap | low |
| MCP-8 | [mcp-server] Unused TokenBudget domain class is dead code | tech-debt | low |
| AG-1 | [agents] OpenCode tools value-check masked by set-drift elif | tech-debt | low |
| AG-2 | [agents] Checker over-requires Copilot handoffs vs the contract | tech-debt | low |
| AG-3 | [agents] Malformed MCP config double-reports in the secret scan | code-improvement | low |
| ~~AG-4~~ | merged into DOC-7 (same template-canon defect) — not filed separately | — | — |
| AG-5 | [agents] Copilot template body 30k cap never checked | test-gap | low |
| AG-6 | [agents] .copilot/README discovery-path prose drifts from the ADR | documentation | low |
| AG-7 | [agents] Duplicated frontmatter parser/constants (checker vs test) | duplicate | low |
| AG-8 | [agents] Skill discovery union vs OpenCode-only mismatch | tech-debt | low |
| AG-9 | [agents] Skill bodies duplicated across hosts with no body-parity gate | duplicate | low |
| DOC-1 | [docs] "test is pointer-only" rationale is wrong across contract + code | documentation | medium |
| DOC-2 | [docs] PR-briefs index stops at PR-13 (PR-14..PR-20 shipped) | documentation | medium |
| DOC-3 | [docs] Dev-guide as-of-PR-N headers disagree (PR-15/16/20) | documentation | low |
| DOC-4 | [docs] Implementation-tour migration walk omits 0007/0008 | documentation | low |
| DOC-5 | [docs] Architecture retrieval_event sketch omits status column | documentation | low |
| DOC-6 | [docs] Namespace table omits graph.get_neighbors | documentation | low |
| DOC-7 | [docs] Composition table implies a nonexistent template manifest | documentation | low |
| EV-1 | [evals] Baseline "regressed" verdict never fails the run | bug | high |
| ~~EV-2~~ | REFUTED — comment is correct (agent_task_cases 04/05) — not filed | — | — |
| EV-3 | [evals] cases.py + budgets.py comment misstates the fallback allowance | documentation | medium |
| EV-4 | [evals] File/symbol/test recall uses lenient substring matching | test-gap | low |
| EV-5 | [evals] semantic_cache_hit_rate denominator includes denials | tech-debt | low |
| EV-6 | [evals] FakeSearchClient ordering unstable on equal scores | tech-debt | low |
| EV-7 | [evals] Agent-subject naming drift vs the budget rule is undocumented | documentation | low |
| EV-8 | [evals] No committed case exercises a real budget denial | test-gap | low |
| ~~INF-1~~ | merged with MCP-2 (same pyright-strict gap) — not filed separately | — | — |
| INF-2 | [infra] .mcp.json references pre-restructure apps/ path and wrong port | documentation | medium |
| INF-3 | [infra] settings.json migration deny glob matches no real filenames | tech-debt | low |
| INF-4 | [infra] CI uses the shared postgres DB instead of agentic_kb_test | test-gap | low |
| INF-5 | [infra] migration-writer subagent uses bare alembic, not uv run | documentation | low |

> **Post-verification merges/drops**: MCP-2 ≡ INF-1 (one issue, type `tech-debt`); AG-4 absorbed into
> DOC-7; EV-2 refuted (not filed); INF-6 informational (not filed).

That leaves **40 distinct proposed issues** to file (2 high · 8 medium · 30 low).

## Recommended filing approach

1. Create the missing labels.
2. File the **2 high + 8 medium** first (the actionable backlog), then the 30 low items — or file all
   at once. Each issue uses the templated body (summary · evidence · why · fix · audit-key · provenance).
3. Open one **tracking issue** labeled `opus-audit` linking all of them and pointing back to this report.
