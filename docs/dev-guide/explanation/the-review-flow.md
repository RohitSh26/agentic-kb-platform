# The review flow

The platform can draft a code review for any pull request — four specialist perspectives,
reconciled into one document — but it cannot publish one. This page explains that design: what
the draft engine does, why it is structurally unable to post to GitHub, and why the publishing
step belongs to you. The hands-on walkthrough is
[tutorial 4](../tutorials/04-review-a-pull-request.md).

## One bounded job per pull request

A draft run is a single, bounded pipeline:

1. **Load the PR** — metadata and diff, read-only.
2. **Four lenses fan out in parallel** — bug, security, quality, and test coverage, each an
   independent reviewer with its own instructions.
3. **Deterministic reconciliation** — duplicate findings are merged, disagreements are kept
   explicit (a severity dispute is recorded, not averaged away), and everything is ranked by
   severity.
4. **Synthesis** — a final pass writes the overall verdict and summary.
5. **Store the draft** — one document lands in the draft store. That is the end of the pipeline.

The reviewers' instructions are not baked into the service. Each lens loads its instruction body
at runtime from the canonical role manifests in `agents/` — edit those files and the panel's
voice changes without touching service code.

## Why it cannot post

"Never posts to GitHub" is not a policy the engine follows; it is a capability it does not have
(ADR-0031):

- **No write credential exists.** The service's GitHub adapter can only fetch — PR metadata and
  the diff. There is nothing in the service that could authenticate a write.
- **There is no posting step.** The pipeline's terminal node stores the draft. No node posts,
  comments, approves, or requests changes — and a contract test pins the graph's shape so a
  posting node cannot appear unnoticed.
- **Nothing triggers it automatically.** There is no workflow that runs the engine on push. You
  run it from your shell, or from automation you set up yourself.

The consequence is accountability without ambiguity: every review that appears on a pull request
was published by a person, under that person's own authorization, after reading it. The engine's
verdict is advisory, always.

## The draft is data you own

A draft is a single document built to be edited, not obeyed:

- an **advisory verdict** (`approve` or `request_changes`) and per-lens verdicts;
- **findings**, each with severity, evidence ids, the lenses that raised it, any explicit
  disagreement, and a `suggested_comment` written to be reworded or dropped;
- deduplicated **open questions**;
- an editable **`summary_markdown`**, always labelled a draft;
- **provenance** — engine version, model, lenses — so you know exactly what produced it.

Drafts are keyed by the PR's head commit. Ask again for the same code and you get the stored
draft instantly, with zero model calls and no model credentials needed. Push a new commit and the
key changes, so the next run computes a fresh draft; the old one stays queryable.

## Paying once, even through crashes

A draft run checkpoints as it goes, and the draft key doubles as the checkpoint thread. A run
killed halfway resumes on the next invocation with its completed lens calls intact — you never
pay twice for the same head commit. If two runs race on the same key, the first stored draft
wins and the second simply reuses it. (Durability requires the engine's Postgres store; without
it, the engine says so plainly in its logs and a crash loses the partial run.)

## Honest validation

Each lens's output must validate against the findings schema. An output that fails validation is
retried once, with the validator's error fed back verbatim; if it fails again, the run fails.
The engine never stores unvalidated output and never papers over a model that cannot produce the
contracted shape.

## A deliberately separate system

The draft engine never touches the Knowledge Registry. It owns exactly one Postgres schema —
`review_panel`, holding its checkpoints, traces, and drafts — and imports nothing from the other
services. It can optionally make one budgeted `kb_search` call to give all four lenses shared
codebase context; that result arrives fenced and untrusted, and a KB failure degrades softly to
a KB-less review.

Full shapes and guarantees: [review-panel contract](../../contracts/review-panel.md).

## Related

- [Tutorial: review a pull request](../tutorials/04-review-a-pull-request.md) — run it, edit it,
  publish it.
- [Reference: tools](../reference/tools.md) — `get_review_draft`, the read-only fetch tool.
- [Observability](observability.md) — the trace every draft run leaves behind.
