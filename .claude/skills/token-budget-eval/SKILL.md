---
name: token-budget-eval
description: >
  Workflow for measuring and tuning context/token efficiency against the benchmark. Use to check a
  retrieval or broker change, set budgets, or tune the semantic-dedupe threshold. Triggers: "check
  token cost", "tune budgets", "evidence reuse rate", "is dedupe working".
---

# Token-budget evaluation

Token saving is an architectural behavior; measure it, don't assume it.

1. Run the eval harness via the `eval-runner` subagent over `evals/`.
2. Read these metrics (defined in docs/architecture §Observability):
   - `context_tokens_per_successful_task` — primary efficiency metric.
   - `duplicate_context_tokens` — waste from repeated retrieval.
   - `evidence_reuse_rate` — are Evidence Packs actually being reused?
   - `semantic_cache_hit_rate` — is dedupe effective?
   - `retrieval_calls_per_agent` — who over-retrieves?
   - `unsupported_claim_rate` / `missing_context_rate` — trust + honesty.
3. Compare against V1 target budgets:
   - Full run: 12k–18k tokens · Initial Evidence Pack: 6k–8k · impl agent: 2 reqs / 3k–4k ·
     test agent: 1 req / 1.5k–2.5k · reviewer: 1 req / 1.5k–2.5k · delivery: 1 req / 1k–1.5k ·
     ≤3–5 evidence cards per retrieval after rerank.
4. Tune the semantic duplicate threshold starting ~0.88–0.92 using the logs; record the chosen value
   and its effect on hit rate vs. false reuse.
5. If a regression is real, file it for a fix — never weaken an assertion to make the number look good.
