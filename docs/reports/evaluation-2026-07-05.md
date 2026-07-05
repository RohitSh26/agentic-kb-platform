# Consolidated evaluation — first full run (2026-07-05)

> House style: real numbers from real runs, variance and failures reported, nothing spun.
> Runner: `evals/run_all.py --with-gates --t3-full` (design: `docs/architecture/evaluation-system.md`).
> Three runs were needed to reach a clean pass — **the two failed runs are findings, not noise**,
> and are documented below. Final run: git `bacb9e3`, overall **PASS**.

## Final scoreboard (run 3, all tiers armed)

| Tier | Question it answers | Status | Key numbers |
|---|---|---|---|
| T0 | Is the code correct? | **PASS** | `make verify` end-to-end: ruff + format + pyright + pytest ×4 projects (36.9s) |
| T1 | Does retrieval find the right things? | **PASS** | 15/15 cases · golden recall 1.000 · 0 ACL leaks · baseline verdict: flat |
| T2 | Fast and right on real data? | **PASS** | alias golden **25/25 (100%)** · latency p50 1.58s, p95 2.30s, 0 errors |
| T3 | Better off with our tools? | **PASS** | tooled 0.342–0.492 vs raw **0.000** (see variance note) |
| T4 | Can someone attack it? | SKIP by design | fixtures live in service suites; executed inside T0 |

Environment: full-docs KB `local.20260705T063732Z` (6,798 artifacts incl. doc artifacts for the
first time; 3,318 aliases; 556 repo-stamped sources; **0 extractor errors** on Groq vs 723 on the
local-model attempt), reader `llama-3.3-70b-versatile` (Groq), exclusive eval-proof test DB.

## T3 in detail — two same-config runs, honest variance

| Run (git) | Tooled coverage | Raw coverage | Flakes (of 20 arms) | Tooled tokens | Raw tokens |
|---|---|---|---|---|---|
| `738d44e` | **0.492** | 0.000 | 3 | 4,951 | 2,344 |
| `bacb9e3` | **0.342** | 0.000 | 6 | 4,752 | 1,692 |

- **Raw has now scored 0.000 in every live run ever performed** (four runs, 40 raw arms across two
  reader models and three KBs — not one expected file found; traces show invented directory trees
  every time). The correctness case for the KB is no longer a finding; it's a law of this dataset.
- **Run-to-run variance is flake-driven**: a flaked arm (model hallucinates a nonexistent tool name,
  provider 400s) scores 0, so 3 vs 6 flakes swings the mean. The stronger 70b reader improved on the
  8/20 weak-reader baseline (→3–6/20) but didn't eliminate it. This is precisely what the queued
  runtime generate-and-test work (bounded provider-400 retry, task #29) targets — today's numbers
  are its baseline.
- **The token-savings arm of A6 remains unmet and unspun**: tooled spends ~2–3× raw's tokens; raw
  is cheap because it fails instantly and finds nothing. Tokens-per-correct-scope: tooled finite,
  raw infinite, unchanged since June.

## What the evaluation system caught while being run (the real yield)

| # | Defect | Found by | Fixed |
|---|---|---|---|
| 1 | Format drift in 21 files — check-only lint runs had passed for days while `make verify`'s format gate would not | run 1, T0 | `738d44e` (+ one reformat-exposed E712 → SQLAlchemy-idiomatic `.is_(False)`) |
| 2 | Stale eval baseline — 2026-06-15 baseline predated the trust/ACL golden cases; tokens-per-successful-task moved 46.93→238 by case-set composition, not regression | run 1, T1 | re-baselined with justification, `738d44e` |
| 3 | `make verify` was never safely runnable as one chain — kb-builder's suite downgrades the shared test DB on teardown, poisoning mcp-server (189 errors) and T1 behind it | run 2, T0+T1 | `bacb9e3` (test targets now depend on migrate-test-db) |
| 4 | A fork bomb in the runner's own first draft (T1's spawned pytest re-entering the runner) | construction-time verification | `1987f2c` (env-pure tiers + inner-run guard, regression-tested) |

Four real defects in three runs, none of which feature work would have surfaced. This is the
eval-first sequencing argument settled empirically.

## Deltas vs the 2026-07-03 report

- KB: code-only → **full docs** (Groq docify, 0 errors); conventions node has real inputs for the
  first time.
- Reader: weak Groq Llama → 70b; flakes 8/20 → 3–6/20; tooled coverage 0.400 → 0.342–0.492.
- One command (`make eval-all`) now produces this entire picture; skips carry reasons; failures
  carry verbatim output.

## Addendum (same day, post-run): KB noise discovered by the bootstrap work

The full-docs KB this evaluation ran against (`local.20260705T063732Z`) contains **~566 spurious
"card" artifacts from 627 fake `ado_card` sources**: under `--backend local`,
`sources.example.yaml`'s ado_card source is *warned about* as "will be skipped" but is not actually
filtered at runtime, and (having no path selection) it matched every workspace file — Groq then
"extracted" cards from arbitrary files. This also retroactively explains part of the 2026-07-03
75.8% extractor-error gate trip (same spurious inputs, against a 404ing model). Impact on this
report's numbers: the eval ran with ~8% noise artifacts polluting the search space, so T2/T3
results stand as **lower bounds** — a clean rebuild may improve them, and will be used for the next
run. Fix tracked: make the local backend actually skip non-fetchable sources (validator warning →
runtime behavior), then rebuild.

## Standing follow-ups fed by this run

1. Task #29 (provider-400 bounded retry) — measured baseline: 3–6 flakes/20 arms.
2. T3 case set is 10 cases — variance would shrink with ~30; add cases per the design doc's
   "adding a case" recipe as usage reveals real task phrasings.
3. Ledger telemetry rows written by eval probes (`agent_name='eval-t2-latency'`) accumulate in eval
   DBs; harmless, documented in the design doc.
