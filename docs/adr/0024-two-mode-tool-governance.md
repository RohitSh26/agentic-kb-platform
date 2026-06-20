# ADR-0024 — Two-mode tool governance: the model gets no shell; the runner owns execution

## Status

**Superseded by ADR-0025 (2026-06-18) and ADR-0026 (2026-06-20).** The no-shell/RPC-only direction
here proved to be more *gating*, which makes the model stuck or greedy. ADR-0025 keeps the model's
native tools (KB-preferred, not gated); ADR-0026 controls token cost by **compressing** what the
model reads rather than restricting access. Retained for the historical reasoning trail.

Accepted (2026-06-18). Driven by an external second-opinion architecture review (the "judge",
2026-06-18) commissioned after a developer report that the in-IDE orchestrator was *slower than a
plain coding agent*. Sharpens ADR-0021 (human-approval delegation) and ADR-0022 (code-owned routing)
by fixing the **tool surface** each agent is given. Does not change the Context Broker contract or the
storage invariants.

## Context

A developer transcript showed the orchestrator, running inside a permissive IDE harness
(OpenCode / a Codex-style host), ignoring the broker and manually walking the working tree —
`Check repo status` → `Scan symbols` → `Read core base` → `Dump async … base` → *"switching to
direct file reads … avoids index gaps"*. It did a full plain-agent investigation **plus** broker
calls: strictly slower and less governed than a plain agent, and a direct violation of invariant 3
(*token saving is enforced by the Context Broker, not by prompts*) and invariant 6 (*agents never
touch data stores directly*).

Locking the agent to broker-only tools (`context.*`) stopped the roaming, but surfaced the real
question: these agents still need to **do work** — edit files, run tests, perform git operations —
and the harness's single "terminal" switch is too coarse. Re-enabling a shell brings back `cat`/grep.

A first proposal was a deny-by-default **shell command allowlist** (allow `pytest`/`ruff`/`git add`…,
deny `cat`/`grep`/`git show`…). A blunt external review rejected it as **security theater**, on a
point that is correct and decisive:

> Any model-controlled execution with observable output is a read channel. A command is not safe
> because its name is allowed. `pytest` is a read channel: a model with write access can author
> `def test_x(): print(open("secret.py").read())`, run it, and read the file out of stdout. The
> same is true of `python -c`, linters, type-checkers, tracebacks, coverage, and git object access.

So the dangerous capability is not "the shell"; it is **the model observing the output of code it
controls**. A command allowlist cannot close that, and shipping one creates a *fake* governance
boundary that is worse than none.

Separately, the review confirmed that *for this product* the broker-only-reads rule is the right
hill to die on. A free-roaming local agent (Cursor/Aider/Claude Code) is faster and fine for a
single developer on a local repo — but our product thesis is a **governed, token-budgeted,
ACL-enforced, audited multi-agent runtime**. If the model can read the tree freely there is no
observability, no ACL on `grep`, no token control, and no audit — the broker degrades to an optional
search helper and the thesis dies.

## Decision

### 1. Two explicitly separate modes, separately labeled.

- **Governed Runtime mode (the product).** The model is treated as an untrusted sandboxed function.
  It gets the broker `context.*` tools plus **structured edit-proposal RPC tools, and nothing else**:
  no shell, no `read`/`grep`/`find`/`ls`, no direct `git`, no ability to choose or run a command. The
  **deterministic runner is the only entity that speaks to the OS.** It owns workspace checkout, file
  reads, the writable-file set, file writes, the git worktree, `git diff`/`status`, `pytest`, `ruff`,
  `pyright`, output filtering, token accounting, ledger events, the retry loop, and the final patch.
  This mode — and only this mode — carries the governance claims (broker-only knowledge, ACL-filtered,
  token-budgeted, fully audited).

- **Convenience IDE mode (optional, non-governed).** A team may run an IDE agent with native tools and
  the broker available as a helper. This mode is shipped and documented as **NOT broker-only, NOT
  ACL-enforced, NOT audit-complete, NOT token-guaranteed.** We do not sell it to security-conscious
  customers as governed. The two modes get separate names, docs, and claims; they are never blended.

### 2. The model never gets a shell, in any governed role.

All effects are RPC calls the runner executes. The model decides *what the code should be*; the runner
decides *what happens to the filesystem*; the broker decides *what code the model may learn*.

### 3. Runner-mediated execution outputs are sanitized before the model sees them.

Because a model-authored test can print arbitrary file contents, the runner must not pipe raw
`pytest`/lint/type-check stdout+stderr back to the model. It returns a **structured, bounded summary**:
pass/fail, failing test ids, exception type, assertion message, `file:line` frames, and a small
sanitized excerpt. Output that looks like a bulk source dump or unrelated content is truncated and
marked `output_suppressed_due_to_content_policy`. (Today `agent_runner._run_pytest` returns the raw
`output[-3000:]` — this is the exfiltration channel this ADR closes.)

### 4. Per-role tool surface (Governed Runtime mode).

| Role | Broker (knowledge) | Effect tools (RPC; runner executes) | Denied |
|---|---|---|---|
| **orchestrator** | `context.create_pack`, `context.create_change_pack`, `context.expand`, `ledger.list_retrievals` | `delegate_task` | disk, shell, edit, git, pytest, raw file reads |
| **implementation** | `context.open_evidence`, `context.expand`, `context.request_more` | `propose_file_replacement(path, full_content)`, `request_test_run(selector)`, `request_static_check(id)` | shell, `git`, `pytest`/`uv`/`python` directly, `read`/`grep`/`find`, `create_pack` |
| **test_author** | `context.open_evidence`, `context.expand`, `context.request_more` | `propose_file_replacement`, `propose_new_test_file`, `request_test_run` | same as implementation |
| **code_reviewer** | `context.open_evidence`, `context.expand`, `context.verify_answer`, `context.request_more` | `get_diff_status` (read the proposed change + summaries) | any write, shell, git, pytest, raw file reads |

The implementer/test-author receive the full contents of **selected** files *from the runner* (sourced
through the broker / change-pack), never by reading the filesystem. New files and dependency-file
promotions must be pre-authorized by the change pack.

### 5. Runner command surface is fixed templates, not model-chosen arguments.

The runner runs only exact templates (`uv run pytest <selected ids>`, `uv run ruff check <changed>`,
`uv run pyright <changed>`, `git diff --stat`/`--name-only`/`--check`, `git status --porcelain`). The
model submits `{"tool":"request_test_run","selector":"targeted"}`; the runner maps it. The model can
never pass `-s` / `--capture=no` / `python -c` / `bash -lc` / `git show` / `git log -p`.

## Consequences

- The governed product becomes honest and airtight: the broker is provably the only source of code
  knowledge, because there is no other channel — not even a weaponizable test.
- Much of this already exists: the BUILD-lane runner emits full files (no shell), applies in a
  worktree, and runs targeted pytest. The deltas are (a) **sanitize `_run_pytest` output** (§3),
  (b) **formalize the per-role RPC surface** (§4), and (c) **relabel the in-IDE manifest path as
  Convenience mode** (§1) instead of implying it is governed.
- The orchestrator manifest prose tightened on 2026-06-18 ("Use ONLY the Evidence Pack — never roam")
  remains as belt-and-suspenders, but is explicitly **not** the enforcement mechanism — the tool
  surface is.
- We accept that Convenience IDE mode exists and is useful, but it is firewalled from the governance
  claims. No customer is told a free-roaming agent is governed.

## Alternatives considered and rejected

- **Shell command allow/deny-list as the main product.** Rejected: security theater. `pytest` and any
  model-authored code are read channels; the allowlist gives false confidence and undermines trust in
  the whole architecture (the central finding of the second-opinion review).
- **Let the governed agent read the filesystem (be "just a faster local agent").** Rejected: it
  abandons the product thesis (governance, ACLs, token budget, audit). Fine for a single-player local
  tool; fatal for a governed enterprise runtime. We keep both, separately labeled.
- **Enforce broker-only via prompts.** Rejected by invariant 3 and by observation: a capable model in
  a permissive harness ignores the prompt. Only removing the tools works.
- **Trust third-party IDE harnesses to expose only broker tools.** Rejected as a *governance* basis:
  if the host also exposes native read tools, the broker is not the single source. Either control the
  tool surface (Governed mode = our runner) or downgrade the claim (Convenience mode).

## Follow-ups

- Harden `agent_runner._run_pytest` (and any lint/type-check feedback) into the §3 sanitized,
  structured summary with the `output_suppressed_due_to_content_policy` guard; add a test that a
  file-dumping test cannot exfiltrate source through the model-visible output.
- Define the §4 RPC tools (`propose_file_replacement`, `request_test_run`, `request_static_check`,
  `delegate_task`, `get_diff_status`) as a versioned contract in `docs/contracts/`, and render the
  per-role surfaces into the manifests + parity contract (extend `check_parity.py`).
- Split the docs/wiki into **Governed Runtime** vs **Convenience IDE** with the explicit non-governed
  labeling for the latter; remove any governance language from the in-IDE manifest path.
- Decide whether Convenience mode ships at all for V1, or is deferred — it is optional and carries no
  thesis weight.
