---
description: Generate or refine a PR brief from the architecture for a feature not yet broken out.
argument-hint: "<short description of the work>"
---

Create a PR brief in `docs/pr-briefs/` for: $ARGUMENTS

Match the existing brief format (Scope, Context with architecture/ADR references, Files to create,
Contracts, Acceptance criteria, Required tests, Do-NOT constraints, and a ready-to-paste kickoff
prompt). Keep it bounded to a single reviewable PR. If the work is too large for one PR, propose a
split into numbered briefs instead of writing one oversized brief.
