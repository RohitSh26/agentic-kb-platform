# PR-42 — Response economy: skeleton evidence text + stable, compact responses (ADR-0033)

## Why

ADR-0033: our measured-45% deterministic code-skeleton compression runs only in the CLI lane;
production tool responses carry raw code text and repeat path strings across sections. Validated
externally (Headroom's deterministic lane + CacheAligner); adopted on our own terms.

## Scope — half (a), kb-builder: skeleton evidence text

- **Ground FIRST**: what `body_text`/`search_text` actually hold for `code_file`/`code_symbol`
  artifacts today (graphify write path), and what `context.open_evidence` / the L0–L2 verify path
  serve from. **Decision tree, in order:** if stored code text is display/search material only →
  skeletonize `body_text` + `search_text`. If any governed path (open_evidence L2, verify quotes)
  contractually serves stored code text as *raw source* → skeletonize ONLY `search_text` and the
  snippet source, leave that body untouched, and record the narrowing in the ADR's implementation
  note. Citation semantics untouched is a HARD gate either way (contract tests must stay green).
- Duplicate the ~200-LOC deterministic compressor from `scripts/codeskeleton.py` into kb-builder
  (ADR-0008: no cross-imports; attribution comment). Python files skeletonized; other languages
  pass through unchanged. Runs at artifact-write time; content-hash caching makes it incremental;
  no migration expected (text columns unchanged).
- Tests: skeleton applied per the grounded decision; non-Python pass-through; incremental skip;
  verify/open_evidence contract tests green; a printed before/after character/token measure on
  real files.

## Scope — half (b), mcp-server: stable, compact responses

- `get_task_context`: **cross-section path dedup** — each path appears once in full (resolved
  scope); blast-radius/similar-changes entries reference paths compactly. This changes the wire
  shape → contract first, `MCP_SCHEMA_VERSION` minor bump (coordinate with PR-41's bump —
  whichever lands second takes the higher number).
- **Determinism discipline** both tools: stable field order, documented sort for every list,
  stable identifiers early / volatile values (timings, budget_used) late. Test: two identical
  requests → byte-identical JSON (modulo the documented volatile tail).
- **Harness compatibility is in scope**: `scripts/eval_task_context.py` and
  `evals/harness/task_context_ab.py` extract surfaced paths from responses — update their parsing
  for the deduped shape so `tool_cover` stays truthful; hermetic A/B tests green.
- Tests: dedup preserves discoverability (every pre-dedup path still recoverable), determinism
  test, payload-size before/after printed.

## Do NOT

- No lossy transforms anywhere near evidence; no ML; no changes to budgets/ledger semantics; no
  raw-document storage changes.

## Acceptance

- [ ] Grounded decision (a) documented in the PR notes; hard gate: all citation/verify contract
      tests green.
- [ ] Both suites + evals green; parity untouched; contract bumped before code.
- [ ] Printed measures: skeleton savings on real artifacts; get_task_context payload bytes
      before/after on the 10 golden tasks.
