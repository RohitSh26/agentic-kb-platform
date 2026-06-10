---
description: Run the full local gate — lint, types, and tests — and report pass/fail concisely.
allowed-tools: Bash(uv run:*)
---

Run, in order, and report each result:

1. `uv run ruff check .`
2. `uv run ruff format --check .`
3. `uv run pyright`
4. `uv run pytest -q`

If anything fails, summarize the failures and fix them, then re-run until green. Do not report a
task as done while any of these fail. Output a one-line final verdict: PASS or FAIL with the count
of remaining issues.
