---
name: kb-first-file-fallback
description: Ask the knowledge base first and cite what it gives you; read specific files directly only when the KB is missing, partial, or stale. kb_search is budgeted — the tool enforces a hard call+token cap, not the prompt — and native read/grep/edit tools are never taken away.
---
# KB-first, file-fallback

The knowledge base is a **preferred, budgeted accelerator** — never a gate (ADR-0025). Native
tools (`read_file`, `read_full`, `list_files`, `grep`, and, for implementers, `edit_file`) are
always available; nothing is removed to force a broker round-trip.

## The discipline

1. **Ask the KB first.** Start with `kb_search` for the task. If a result already answers the
   question or names exactly the right files, use it and cite it — do **not** re-read what search
   already gave you.
2. **Fall back to files on a gap.** If the KB is missing, partial, or stale — or you need exact
   current code to make a change or quote precisely — read those **specific** files directly with
   `read_file` (skeleton: signatures kept, bodies elided, cheap to scan) or `read_full` (the exact
   body, for anything you edit or quote). The KB points at the right place fast; the file read
   gives exact truth.
3. **The budget is enforced in the tool, not the prompt.** `kb_search` carries a per-task hard cap
   — call count and token budget (`max_context_calls`, `max_context_tokens`). You do not need to
   self-police it: once the cap is spent, the tool reports budget exhaustion and you proceed with
   what you have or read the specific files you still need.
4. **A file-fallback is a KB-gap signal, not a failure.** Reading a file directly because the KB
   came up short is expected and logged; it is exactly how the KB improves over time. A failed or
   erroring tool call is treated exactly like a KB gap: never stop, and never report the tool
   failure as your answer — fall back to native tools and answer the developer's question
   completely.

## Why this shape

Routing every read through a broker crippled the model — a plain agent with native tools
outperformed the mandatory `create_pack → expand → open_evidence → verify` pipeline in practice.
The KB stays valuable for what native `grep`/`read` cannot do cheaply — cross-repo search across
code, docs, and tickets in one call — while the agent keeps its own hands for everything else.
