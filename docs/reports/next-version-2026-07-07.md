# Next version — all discussed enhancements built, verified, measured (2026-07-07)

> The owner paused the pilot pending "the next version of the tools with all enhancements
> discussed." This report is the single deliverable: what was built, the proof it works, and the
> measurements — honest ones — of what it bought. Final eval: **overall PASS, all tiers**, at
> git `df0fd72` + this report.

## What shipped (all committed and pushed to main)

| Enhancement | Commit | One line |
|---|---|---|
| PR-41 `get_review_draft` | 652e8e8 | Review drafts fetchable over MCP from any host — read-only, compute-never, ledgered, zero budget charge; code_reviewer granted |
| PR-42 response economy (ADR-0033) | 13a1fdd | Skeleton evidence text fills code_file search text (45% measured savings; body_text untouched — quote-grounding preserved); get_task_context gains a deduped path table + byte-identical determinism (schema 1.12.0) |
| PR-43 learn loop (ADR-0034) | df0fd72 | kb_search misses mined into alias candidates at build time — deterministic, ACL-never-widened, golden-set-proven-unaffected; dashboard gains the mined-vs-unresolved split |
| Provider alignment (#38) | a22dd71 | All three LLM_PROVIDER consumers drift-tested; review-panel gains anthropic_foundry; kb-builder gains GROQ_API_KEY fallback |
| Embeddings validation (#39) | a22dd71 | EMBEDDINGS_PROVIDER validated ollama\|openai with a real /v1/embeddings implementation; unknown values fail loudly |
| Docs restructure | fb15531 | Task-first USING track (01–08, incl. the psql/backup cookbook — every command run live) split from CONTRIBUTING (20–22); zero broken links across 210 files |
| Hardening en route | 8c89d6d, 8ac890b, 652e8e8 | Parity YAML armor (hosts can't silently drop agents); schema-rejected calls ledgered; parity scanner immune to vendored junk |

## The proof (final evaluation, next-version KB `local.20260707T203326Z`)

| Tier | Result | Key numbers |
|---|---|---|
| T0 gates | **PASS** | make verify end-to-end; suites: kb-builder 584 · mcp-server 571 · review-panel 112 · evals 181 (+ scripts 36) |
| T1 golden | **PASS** | 15/15 · recall 1.000 · 0 ACL leaks · baseline flat |
| T2 live KB | **PASS** | alias **25/25** · latency **p50 0.868s** (was 1.58s pre-enhancement, 0.93s mid) · 0 errors |
| T3 A/B | **PASS** | see the honest three-run table below |
| T4 adversarial | in gates | by design |

Skeletons live: 257 `code_file` artifacts now carry skeleton search text; `referenced_paths`
dedup measured **−7.0%** payload on the golden tasks (up to −14.4% on blast-radius-dense cases).

## T3 tokens — the honest three-run table (same config: 70b reader, fresh KB)

| Run | Tooled cov | Tooled tok | Raw cov | Raw tok | Flakes (t/r) | Retries recovered |
|---|---|---|---|---|---|---|
| eval-all pass | 0.450 | 5,556 | 0.000 | 6,540 | 5 total | n/r |
| detail A | 0.383 | 4,660 | 0.000 | 2,032 | 2/4 | 0/0 |
| detail B | 0.483 | 4,316 | 0.000 | 1,796 | 3/5 | 3/1 |

**Verdict on the token-savings arm, stated plainly:** the eval-all run showed the first-ever
crossover (tooled cheaper than raw), but it did **not replicate** — raw-arm totals swing 3.6×
run-to-run because they're dominated by model flakes and retry survival, and n=10 with 20–50%
flake rates cannot support a stable end-to-end token claim. What IS stable across seven
consecutive live runs: **raw coverage 0.000 every single time** (tokens-per-correct-result:
tooled finite, raw infinite), tooled coverage 0.38–0.49, and the response-level savings are
real and deterministic (45% skeleton, −7% payload). The bounded-retry loop demonstrably rescues
arms now (`retries_recovered=3` in run B — arms that died in the 07-05 baseline).

## The learn loop, live

First production run: `misses_seen=0` — the ledger's real query history contains **no genuine
misses to mine** (every recorded developer/test query resolved). A deliberately obscure synthetic
probe ("zombie writer fence circuit breaker recipe") **hit 5 real artifacts** — the gap the loop
exists to close doesn't currently exist on this corpus, which is the KB-coverage result you want.
The mechanism is proven by 17 seeded tests + live wiring (counters on `kb_build_run`, dashboard
split rendering); it will fire when real usage produces real gaps.

## Not built, by design

- `headroom wrap` host-side experiment — owner-gated (#43), untouched.
- Azure Search projection of skeleton-bearing `code_file` (Azure lane is deferred); nested
  `schema_version` repetition (next response-economy pass candidate); `aliases` graph edges for
  ledger-mined rows (contract-documented open question).

## Bottom line

Every enhancement discussed is built, independently verified, pushed, and measured. The platform's
provable value proposition after this version: **correctness and grounding (unbeaten in every
measurement ever taken), sub-second task context, self-hosted observability to the row, a
self-correcting alias index, and deterministic response economy** — with token-total parity
approached and honestly reported as not-yet-stable rather than claimed.
