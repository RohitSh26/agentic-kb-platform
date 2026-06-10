---
description: Load the next unstarted PR brief and begin it using the implement-pr workflow.
argument-hint: "[optional PR number, e.g. 04]"
---

Determine the target PR: if `$ARGUMENTS` names a number, use `docs/pr-briefs/PR-$ARGUMENTS-*.md`.
Otherwise, list `docs/pr-briefs/`, check `git log` and the repo state to find the lowest-numbered
brief not yet implemented, and pick that one.

Then follow the `implement-pr` skill exactly: load scope, branch, contracts first, guardian review,
implement, migration if needed, tests, `/verify`, self-review, PR description. Do not exceed the
brief's scope. Report which PR you selected and why before starting.
