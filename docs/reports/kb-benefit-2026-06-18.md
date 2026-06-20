# KB benefit report — KB-first vs. plain agent (2026-06-18)

> Honest first pass. Small sample (n=3), a weak/cheap model, single repo. The numbers below are
> real (captured from `scripts/kb_agent.py`), and I have **not** spun them.

## Setup (controlled — only the KB is toggled)

- **Harness:** `scripts/kb_agent.py` — one tool-calling loop, 5 tools (`kb_search`, `read_file`,
  `list_files`, `edit_file`, `run_tests`). Two modes, identical model + task:
  - **KB-first:** `kb_search` available + "KB first, files second" prompt, budget = 4 searches.
  - **Baseline (`--no-kb`):** native file tools only, no KB (the "plain agent").
- **Model:** Groq `llama-3.1-8b-instant` (from `.env`) — small and cheap, which makes the KB's
  effect *more* visible.
- **KB:** active version `local.20260617T171228Z` (~3,700 artifacts: code + docs).

## Results

| Task | Mode | Answer quality | Tokens (in/out/total) | Steps | KB / file calls |
|---|---|---|---|---|---|
| 1. How is the active KB version / build_seq resolved? | **KB-first** | ✅ correct, cited a real file (`test_health.py`) | 4121 / 335 / **4456** | 3 | 1 / 1 |
| 1. | Baseline | ❌ **hallucinated** `ActiveKBSettings.json`, `BuildSequenceConfig.json` (don't exist); gave up | 1260 / 222 / **1482** | 2 | 0 / 3 |
| 2. What fields does `context.request_more` require? | **KB-first** | ✅ **correct** (`question, why_needed, decision_needed, already_checked, max_tokens`), cited | 7050 / 194 / **7244** | 4 | 1 / 2 |
| 2. | Baseline | ❌ failed: invented `/tmp/context/schema.json`, then emitted invalid tool-call syntax (Groq 400) | — | — | — |
| 3. Where is the per-agent token budget enforced? | **KB-first** | ✅ **correct** (`context_broker/state.py`, `budgets.py`, `domain/token_budget.py` — all real) | 3376 / 100 / **3476** | 3 | 1 / 1 |
| 3. | Baseline | ❌ **hallucinated** Java/Mongo files (`com/mongodb/...`, `TokenPoliciesBuilder`) — wrong language entirely | 3068 / 366 / **3434** | 4 | 0 / 6 |

## Findings

### 1. Answering with context & clarity: the KB helps **decisively**. ✅
- **KB-first: 3 / 3 correct, every answer cited a real file.**
- **Baseline: 0 / 3 correct, every answer hallucinated non-existent files** — .NET configs, Java/Mongo
  classes — confident, wrong, and dangerous. The weak model has no idea what this repo contains, so
  without the KB it *guesses*, and an 8B model guesses badly.
- This is the KB's core value: it grounds a cheap model on an unfamiliar codebase and turns
  hallucinated garbage into a correct, cited answer. It also proved more **reliable** (the baseline
  twice failed to even produce a valid tool call / answer).

### 2. Token savings: **No — not at this stage. (Honest.)**
- Raw tokens were **comparable to higher** with the KB (Task 1: 4456 vs 1482; Task 3: 3476 vs 3434).
- The baseline looked "cheaper" only because it **failed fast with garbage**. A wrong answer is not a
  saving.
- The fair metric is **tokens per *correct* answer**: KB-first ≈ **5,059 tok/correct answer**;
  baseline = **∞** (zero correct). On useful output, the KB wins outright — but the headline
  "token savings" number is **not** there yet, and I won't claim it.
- **Why no raw savings:** `kb_search` returns 6 cards (~snippets) = real input tokens, and the model
  still reads a file afterwards. Prompt caching (which makes re-reads ~90% cheaper) is not in play in
  these one-shot Groq runs.

## How this avoids the old Context Broker problems

| Old broker problem | This version |
|---|---|
| Mandatory `create_pack → expand → open_evidence → verify` round-trips | **One** `kb_search` call, then the agent proceeds (2–4 steps total) |
| Model crippled — native tools removed, "switched to direct file reads" anyway | Native `read_file`/`grep` **kept**; KB is preferred, not a gate |
| "Token saving enforced by prompts" (it wasn't) | Budget = an **integer counter in code**; the `kb_search` tool is withdrawn when spent |
| Slower than a plain agent | KB-first finished every task in 3–4 steps; the *baseline* was the one that flailed |

## Caveats (so this isn't oversold)

- **n = 3**, one weak model, one repo. Directional, not statistically strong.
- `kb_search` is keyword + IDF-style ranking only — no embeddings/graph traversal in this MVP.
- No prompt caching; a stronger Groq model (e.g. `llama-3.3-70b`) would likely lift *both* modes.

## To also get token savings (next experiments)

1. Trim `kb_search` output (top 3 tight cards, shorter snippets) and let the agent answer **from the
   cards** without re-reading when coverage is high.
2. Turn on **prompt caching** for the repeated system/KB context.
3. Re-run on `llama-3.3-70b` and on a larger task set (≥10), including code-*change* tasks.

## Bottom line

With a cheap model on an unfamiliar codebase, **the KB is the difference between a hallucinated answer
and a correct, cited one** — that is the value, and it is large. **Token *reduction* is not yet
demonstrated**; today the KB buys *accuracy and grounding*, not cheaper tokens. The path to also
cutting tokens is clear (tighter payloads + prompt caching), but I'm reporting what the data shows
now, not what we hope it shows.
