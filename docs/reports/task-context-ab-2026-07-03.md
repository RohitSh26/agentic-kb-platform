# get_task_context A/B report — tooled vs raw (2026-07-03)

> Honest first pass, house style of `kb-benefit-2026-06-18.md`: real numbers from real runs,
> confounds and limitations stated, nothing spun. Harness: `scripts/eval_task_context.py`
> (two-arm, kb_agent.py-style) over `evals/agent_task_cases/task_context_ab_v1.yaml`
> (10 tasks, expected files hand-written before any run).

## Setup

- **Arms** (same model, same task, same step cap): **tooled** = read tools + ONE
  `get_task_context` call (the real in-process broker path: LangGraph fan-out, alias index,
  blast radius, ledger row written per call); **raw** = read tools only.
- **Reader model**: Groq Llama (via repo `.env`) — deliberately cheap/weak, which makes grounding
  effects visible but also caps both arms (see Limitations).
- **KB**: `local.20260703T071519Z` (built fresh from today's repo; **code + git-metadata only**,
  zero-LLM build — 3,297 alias artifacts, 8,898 `aliases` edges resolving to real code artifacts).

## Results (mean over 10 cases)

| Arm | Expected-file coverage | Tool-surfaced coverage | Steps | File reads | Tokens |
|---|---|---|---|---|---|
| tooled | **0.400** | 0.400 | 1.7 | 2.7 | 2,245 |
| raw | **0.000** | — | 2.0 | 4.2 | 1,893 |

- **The raw arm found zero expected files across all ten cases.** Its traces show confident
  exploration of directories that do not exist (`docs/search_index/indexing/...`,
  `rules/alias-mining/`, `conf/indices/solr/...`) — the same hallucination failure mode the
  June kb-benefit report measured at n=3, reproduced at n=10.
- **Every file the tooled arm covered was surfaced by the tool itself** (`tool_cover` ==
  coverage): the weak reader model contributed nothing beyond the tool's output — and where the
  tool's answer was incomplete, it still anchored the model in the right *real* directories
  instead of inventions.
- Tokens: tooled spent ~19% more raw tokens than raw. On **tokens per correct scope**, the same
  metric the June report settled on: tooled ≈ 5,600 tok/covered-case-equivalent; raw = ∞ (zero
  correct). A cheaper wrong answer is not a saving.

## Per-case results (fresh-KB run)

`ERR@0` = the arm died on its very first model call (hallucinated tool name → provider 400) and
never got to act; its 0.00 is a model flake, not a tool verdict. Flakes split evenly — 4 tooled,
4 raw — so the aggregate comparison is not biased toward either arm.

| Case | Task kind | Tooled cov | Tooled notes | Raw cov | Raw notes |
|---|---|---|---|---|---|
| durable-cache-alias | bug fix, history-echoing phrase | **1.00** | 2 steps, 3 reads | 0.00 | ERR@0 |
| task-context-synthesize | perf tune, history-echoing | **1.00** | 2 steps, **0 file reads** — tool output alone sufficed | 0.00 | ERR@0 |
| publish-quality-gate | guard, history-echoing | **1.00** | 3 steps | 0.00 | ran 2 steps, missed |
| alias-mining-rules | tuning, history-echoing | **1.00** | 4 steps | 0.00 | 9 reads in fantasy dirs |
| search-index-projection | integration wiring | 0.00 | ran; explored the RIGHT real dirs, missed exact files | 0.00 | fantasy `docs/search_index/` tree |
| verify-receipt-signing | prospective feature (doesn't exist yet) | 0.00 | ran 2 steps, 7 reads | 0.00 | ERR@0 |
| kb-search-dual-budget | guard on existing subsystem | 0.00 | ERR@0 | 0.00 | ran 4 steps, missed |
| gitlab-connector | new feature by analogy | 0.00 | ERR@0 | 0.00 | ran 4 steps, missed |
| ledger-run-listing | feature on existing subsystem | 0.00 | ERR@0 | 0.00 | ran 3 steps, missed |
| graph-trust-floor | config change | 0.00 | ERR@0 | 0.00 | ERR@0 |

## By task type — where the KB actually earns its keep

- **History-echoing maintenance** (fix/tune/guard something the repo's commits and briefs already
  talk about): tooled **4/4 cases at 1.00 coverage** — this is the alias index working exactly as
  designed: the developer's phrasing matches mined history, one call returns perfect scope. One
  case needed *zero* file reads — the cheapest possible correct run (2,267 tokens).
- **Novel / prospective work** (the thing doesn't exist yet, or the phrasing has no history to
  match): tooled 0/2 on clean runs — but with a qualitative difference the coverage number hides:
  the tooled arm explored the correct *real* directories (`services/kb-builder/.../alias/`), while
  raw invented entire directory trees. For genuinely new work, the KB narrows the search; it can't
  name files that don't exist.
- **Clean-run means** (excluding step-0 flakes on both sides): tooled **0.667** (4/6), raw
  **0.000** (0/6).
- **Implication for the roster**: the orchestrator's EXPLAIN lane and maintenance-type BUILD tasks
  are where `get_task_context` is decisive today; greenfield tasks lean on the host model's own
  reasoning with the KB as a directional aid — consistent with the tool's design (it returns
  `open_questions` rather than guessing at what doesn't exist).

## Verdict against plan criterion A6

The literal "≥30% fewer tokens at equal-or-better correctness" arm of A6 is **not met** (tokens
were comparable-to-higher). The alternative arm — **meaningfully better correctness at a
comparable token budget** — is met decisively (0.400 vs 0.000 at 2,245 vs 1,893 tokens). This is
consistent with everything this platform has measured to date: the KB's demonstrated value is
**grounding**, not raw token reduction; token savings require a reader model strong enough to
trust-and-stop (see next steps).

## What the eval surfaced beyond the numbers (arguably the bigger win)

1. **A real PR-38 defect, caught by the platform's own publish gate.** The first full build after
   PR-38 landed failed `edge_evidence_integrity`: all 8,898 alias edges were flagged because
   `aliases` was in the relation-ontology *contract* but missing from `ALLOWED_EDGE_TYPES` — the
   enforcement-side copy in `publish_gates.py`. The build correctly refused to activate
   (invariant 5). Fixed same-day; suite green.
2. **Harness hardening.** The Groq model hallucinates nonexistent tool names often enough
   (8 of 20 arm-runs ended early on provider 400s) that the eval script needed the same guard
   `kb_agent.py`'s loop already had — one flaky generation now costs one arm its remaining steps,
   flagged `model_error`, instead of killing the whole eval.
3. **A DX bug, tracked**: `config_loader` hard-requires `token_env` resolution for sources the
   local backend then skips anyway — cost two build attempts.
4. **An ops finding**: docify on local Ollama `qwen2.5-coder:7b` runs ~35s/doc (~7h for this
   repo's docs) — fine for a nightly, not for interactive rebuilds. The default docify model
   (`llama3.1`) also wasn't pulled locally, which a first run surfaces as a 75% extractor error
   rate — again caught by a publish gate, never served.

## Limitations (read before quoting the numbers)

- **Weak reader model dominates both arms**: 8/20 early-ends on hallucinated tool calls; one run
  read literal `/path/to/...` placeholder paths. A stronger reader would lift both arms — the
  *delta* (grounded vs hallucinated) is the durable finding, not the absolute 0.400.
- **Code+commits-only KB**: doc artifacts were excluded (docify too slow locally, see above), so
  conventions/doc-expecting aspects of some cases were structurally uncoverable.
- Golden cases were hand-written against this repo by the PR-39 author — fit-to-source, not a
  generalization estimate.
- An earlier run against a 17-day-stale KB (pre-PR-38/39 content) scored tooled 0.100 / raw 0.000
  — reported here for completeness; the fresh-KB run above supersedes it.

## Next steps

1. Re-run with a stronger reader model (set `LLM_PROVIDER`/`LLM_MODEL`; the harness is
   provider-agnostic) — the A6 token arm is only fairly testable with a model that can act on
   surfaced context without babysitting.
2. Nightly-build the full KB (docs included) on infrastructure where docify isn't 35s/doc, then
   re-run to give the conventions node real inputs.
3. Feed the 8/20 tool-name-hallucination rate into the host-manifest guidance (smaller tool
   surface for weak models).
