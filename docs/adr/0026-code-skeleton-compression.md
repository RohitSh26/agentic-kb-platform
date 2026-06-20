# ADR-0026 — Code-skeleton compression: shrink what the agent reads, never gate it

## Status

Accepted (2026-06-20). Complements ADR-0025 (KB-first/file-fallback). Inspired by a deep-dive of
the open-source **Headroom** project (`chopratejas/headroom`, Apache-2.0) — we recreate the *idea*
(reversible context compression) on our own stack, not the code, and deliberately **not** its
trained ML text-compressor (a lossy ~300 MB ONNX model, the wrong fit for a provenance-first KB).

## Context

Two facts collided across this project's measurements:

1. The token sink for a coding agent is **reading whole files**. A handful of file reads dwarfs the
   search/answer tokens.
2. Every attempt to control that by **gating** the model (mandatory broker flow, no-shell/RPC,
   `kb_search` budgets) made the model **stuck or greedy** — block it and it can't work; let it
   roam and it over-reads. Restriction fights the model. (The whole ADR-0024 → ADR-0025 arc.)

The resolution is to attack cost from the *other* side: don't restrict *what* the model reads —
make *what it reads* **cheap**. To orient in a codebase and write code that fits, the model mostly
needs **structure** (imports, class/function signatures, type hints, a one-line purpose), not the
full **bodies** of neighbouring code. So compress a file to its **skeleton**: keep the shape,
elide the bodies. Greed becomes affordable instead of forbidden, and the model is never blocked.

Measured (deterministic, `docs/reports/compression-benefit-2026-06-20.md`): across 80 real
mcp-server source files, skeletonization saves **41% of tokens overall and 60–80% on the large
files** an agent actually reads.

## Decision

1. **A deterministic, reversible code compressor** (`scripts/codeskeleton.py`). Python uses the
   `ast` module to keep signatures + decorators + first docstring line and replace each function
   body with `... # N lines elided` (the skeleton stays valid Python). Non-Python / unparseable
   files use a language-agnostic line heuristic (keep top-level + structural/signature lines,
   collapse indented bodies). Pure rules, **no ML, no network**, same input ⇒ same output.

2. **Reversible — compress for THINKING, never for CITING.** The compressor never mutates the
   original; the exact file is always one `read_full` away. The agent reads the skeleton to orient,
   then pulls the exact body of the 1-2 things it actually edits or must quote. Skeletons must
   **never** back a verbatim citation.
   - **Where provenance is ENFORCED:** the broker path. Its L0 quote-grounding verifier
     (ADR-0011 / `verification-receipt.md`) checks every quoted claim against the original artifact
     text, so a skeleton can never become a cited claim there.
   - **Where it is NOT yet enforced:** the `scripts/kb_agent.py` experiment. That loop does **not**
     call `context.verify_answer` (it is a standalone demo for the code-*writing* use case, where
     exact citation is explicitly not required). The provenance guarantee for the skeleton/full
     split is therefore **conditional on wiring the verifier in before this graduates from
     `scripts/`** — tracked as a follow-up below and a TODO in the code. Do not present a
     `kb_agent.py` answer as citation-grade until that is done.

3. **Skeleton-by-default reads, full-on-demand.** In the agent (`kb_agent.py`) `read_file` returns
   the skeleton for code; a sibling `read_full` returns the exact text. The model is **never gated**
   — it can read anything; everything code-shaped just arrives smaller.

4. **Honest measurement.** Token savings are reported deterministically (a pure function over real
   files), not from a single LLM run. End-to-end A/B on a flaky tool-calling model is explicitly
   labelled untrustworthy until re-run on a reliable model.

## Consequences

- The model keeps all its native tools and is never blocked or budgeted into uselessness; cost is
  controlled by **shrinking payloads**, not by **restricting access**. This is the resolution to the
  stuck/greedy dilemma that gating could not solve.
- ~41% fewer tokens to read the codebase overall (60–80% on big files), deterministically, with the
  exact original always recoverable.
- Compression is **lossy by design** (bodies dropped) — acceptable for orientation, forbidden for
  citation. The reversible `read_full` path preserves exact-text guarantees.
- We own a small, dependency-free compressor (~200 LOC + tests) instead of vendoring Headroom or its
  ML model. If we later need prose/JSON/log compression, add deterministic compressors for those
  content types before reaching for any ML model.

## Alternatives considered and rejected

- **Vendor Headroom / its trained text-compressor.** Rejected: a ~300 MB lossy ONNX model is the
  wrong fit for a provenance-first KB where exact citations matter, and vendoring its internals is a
  maintenance burden (the same reasoning as [[reuse-and-judge]] / the docify wrapper decision).
- **Gate the model to save tokens** (budgets / no-shell). Rejected: makes the model stuck or greedy
  (the ADR-0024 → ADR-0025 lesson). Compression saves tokens *without* restricting the model.
- **Compress everything, including cited text.** Rejected: lossy compression cannot back a verbatim
  quote; citations always read the reversible original.

## Follow-ups

- Re-run the end-to-end token A/B on a reliable tool-calling model (Claude / GPT-4-class) for a clean
  paired number; the deterministic corpus number stands regardless.
- Before `kb_agent.py` is presented as citation-grade, wire a verification step (call the broker's
  `context.verify_answer`, or an equivalent quote-against-original check) so the skeleton/full
  provenance boundary is enforced, not just documented.
- If the broker path adopts skeletonization, add it as a *skeleton tier* on `context.open_evidence`
  with the exact span behind the existing by-handle fetch. (Note: this is gated on the pending
  ADR-0025 reword of CLAUDE.md invariants 3 & 6 — do not wire it until those invariants reflect the
  KB-preferred, not-gated, relaxation.)
- Add deterministic JSON/log compressors for large tool outputs (the other big context sink).
- Promote `codeskeleton.py` into a proper service module with the rest of the agent runtime when the
  experiment graduates from `scripts/`.
