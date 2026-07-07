# The evaluation system — design and entry point

> Companion to `docs/contracts/evals-report.md` (the T1 report/baseline schema) and
> `docs/architecture/00-overview.md` §13. This document describes the evaluation *project* that
> already exists under `evals/`, `scripts/`, and the service test suites as one designed **system**
> with five tiers, a stated purpose for each, and a single consolidated entry point
> (`evals/run_all.py`). It does not replace any of the pieces it describes — it names them,
> explains why each lives where it does, and reports on all of them together.

## 0. What this is

Before this document, "run the evals" meant knowing, from memory, that there were at least four
different runnable things (`evals/run.py`, `scripts/eval_alias_resolution.py`,
`scripts/eval_task_context.py`, and each service's own `pytest`), each with its own preconditions
(a migrated registry vs. a real built KB vs. LLM credentials), and no single place that told you
which of them your current shell could actually run. Nothing here was broken — every piece was
already well-built and well-documented in its own docstring. What was missing was the *system*
view: one map of what exists, why it's split the way it is, and one command that reports on
everything that can run right now.

`evals/run_all.py` is that command. This document is that map.

## 1. Principles

These apply to every tier and to the consolidated runner itself.

- **Verified, not claimed.** A number in a report came from a real run of real code against real
  (or realistically seeded) data. If something wasn't run, it isn't reported as a value — it's
  reported as `not_measured` (T1's metric contract) or `skip` with a reason (the consolidated
  runner).
- **Honest reporting.** Report what the data shows, including negative results. The house style
  is `docs/reports/kb-benefit-2026-06-18.md` and `docs/reports/task-context-ab-2026-07-03.md`:
  state the sample size, state the confounds, state the metric that *didn't* clear the bar next to
  the one that did. A report that only shows wins is not trusted here.
- **Every number cites its source run.** A metric in a curated `docs/reports/*.md` write-up
  names the script, the KB version, and the command used to reproduce it (see every existing
  report's "Setup" / "Build used" section). A metric in the consolidated runner's output names the
  check that produced it and is reproducible by re-running that one check directly.
- **Degradation over failure.** A tier that cannot run in the current environment (no
  `TEST_DATABASE_URL`, no `DATABASE_URL`, no LLM credentials) **skips with a stated reason**. It
  never fails the run, and it never fabricates a number to fill the gap. This is the same
  discipline `run.py` already applies (exit `2` with a clear stderr message when
  `TEST_DATABASE_URL` is absent) generalized across every tier.

## 2. Generate-and-test loops: where they belong and where they are forbidden

The runner and the tiers below produce failure signals. It is reasonable for other automation — an
agent loop, a review-panel lens, a build runner — to want to *act* on those signals. This section
draws the line between two shapes of "try again" so that line is never blurred by accident.

### Adopted: bounded runtime retries against a machine-checkable validator

A **runtime** component may retry, bounded, when a deterministic validator — not a golden set, not
a human — produces an exact, objective error that can be mechanically fed back:

- Schema-invalid LLM output retried **once** with the verbatim validation error appended to the
  next prompt (e.g. a review-panel lens whose output fails its Pydantic schema).
- A provider 400 caused by a hallucinated tool name, fed back to the model as the literal error
  string, for a bounded retry — the same shape `scripts/eval_task_context.py`'s arm-runner already
  uses so one flaky generation costs one arm-run its remaining steps instead of the whole eval
  (`docs/reports/task-context-ab-2026-07-03.md`, "Harness hardening").
- The retry is always **bounded** (a fixed, small count — never "until it passes"), and the
  failure that triggered it is always surfaced **verbatim** — logs and traces are data, never
  replaced by a prose summary of what went wrong.

These are runtime robustness mechanisms. They belong in the product's own agent/review loops
(review-panel's lens execution, the `kb_agent.py`/`eval_task_context.py` model-step guard), **not**
in the evaluation system itself. Both are now built: `kb_agent._model_step` (shared by
`scripts/kb_agent.py` and `scripts/eval_task_context.py`) retries a provider-400 once, feeding the
verbatim error back before giving up — `scripts/eval_task_context.py`'s A/B output carries a
`retries_recovered` counter so a recovered retry is never folded into the `flakes` metric baselined
at 3–6/20 in `docs/reports/evaluation-2026-07-05.md`. `review_panel.graph.nodes._complete_with_schema_repair`
applies the symmetric retry to a schema-invalid lens output, fencing the verbatim validator error
(it can carry fragments of the model's own untrusted-derived prior output) before the one bounded
retry. Tests for both live beside the code they retry (`scripts/test_kb_agent_model_step_retry.py`,
`scripts/test_eval_task_context_retry.py`, `services/review-panel/tests/unit/test_schema_repair.py`,
`services/review-panel/tests/integration/test_schema_repair_retry.py`) — not under `evals/`, per
this section's own boundary.

### Forbidden: iterate-until-pass against a measurement eval

None of T1/T2/T3's golden sets, accuracy numbers, or coverage numbers may ever be treated as a
target to loop against. A golden set (`alias_golden_v1.yaml`, `task_context_ab_v1.yaml`,
`retrieval_cases/*.yaml`) is a **one-shot measurement**: its expected answers were hand-written and
committed *before* any run, specifically so the system under test cannot see them in advance (T1's
"no grading your own homework"). If a build, prompt, or resolver is iterated — automatically or by
a human loop — against the *same* golden set until it goes green, the golden set stops measuring
generalizable capability and starts measuring fit-to-the-test: Goodhart's law, "when a measure
becomes a target, it ceases to be a good measure." Concretely:

- Do not write a loop that reruns `scripts/eval_alias_resolution.py` or
  `scripts/eval_task_context.py`, tweaks the resolver/prompt, and reruns until
  `top1_accuracy`/coverage crosses a bar.
- Do not treat a T1/T2/T3 failure as a validator to retry against in the sense §2's adopted case
  does — a wrong answer on a golden case is a finding to investigate and fix like any other bug
  (report it, including if it's negative — see every `docs/reports/*.md`), not a signal to
  auto-patch until the number improves.
- A model/prompt/resolver change that was iterated against a golden set *during development* is
  evaluated on that set only **once more**, after development is otherwise complete, and that
  result is reported honestly even if it doesn't clear the bar.

### The runner's own contribution: verbatim, machine-parseable failures

Because a legitimate downstream consumer (a human, or one of the adopted bounded runtime loops
above) needs objective data to act on, every check in `evals/run_all.py` keeps two fields
separate on failure (`harness.tier_result.CheckResult`):

- `reason` — a short, human-authored pointer (e.g. `"exit 1"`, or a skip explanation).
- `detail` — the **verbatim** captured subprocess stdout/stderr tail, or the real exception's
  `type(exc).__name__: message`, never a paraphrase.

The consolidated markdown report renders `detail` in a fenced code block, and full detail is always
one step away (`evals/report.json` for T1, the captured subprocess output for T2/T3) rather than
re-derived into a summary that could drift from the source.

## 3. The five tiers

| Tier | Runs where | Needs | Owns | New in this consolidation |
|---|---|---|---|---|
| T0 | every project's own `pytest`/`ruff`/`pyright` | nothing (no creds) | each service + `evals/` | no — invoked via `make verify` |
| T1 | `evals/` | `TEST_DATABASE_URL` (migrated registry) | `evals/run.py` + `evals/tests/` | no |
| T2 | `evals/` + `scripts/` | `DATABASE_URL` (a real built KB) | `scripts/eval_alias_resolution.py` + `harness.task_context_latency` | latency probe is new |
| T3 | `services/mcp-server` + `scripts/` | `DATABASE_URL` + LLM creds | `scripts/eval_task_context.py` | no |
| T4 | inside each service's own suite | nothing (no creds) | `services/review-panel`, `services/mcp-server` | no — documented only |

### T0 — Hermetic per-project gates

**What:** `ruff check`, `ruff format --check`, `pyright`, and `pytest` for each self-contained
project (`services/kb-builder`, `services/mcp-server`, `services/review-panel`, `evals`) — exactly
`make verify`.

**Why it exists:** this is table-stakes correctness, not a retrieval/agent-quality evaluation. It
answers "is the code correct," not "does the system's retrieval or graph behavior meet a quality
bar." It needs no credentials and no built KB, and it already runs in CI on every push.

**Where it runs:** everywhere, always — but the consolidated runner treats it as **opt-in**
(`--with-gates`) rather than a default, because it duplicates CI and is comparatively slow to
repeat locally (four `uv`-managed projects, each with its own venv sync). When it does run, it
subsumes T4 (see below) — the adversarial fixtures live inside the very suites `make verify`
already runs.

### T1 — Deterministic golden sets

**What:** `evals/run.py` (the `retrieval_cases/*.yaml` + `agent_task_cases/*.yaml` benchmark set,
plus the golden-query publish gate over `retrieval_cases/golden/*.yaml`) and the hermetic pytest
suite in `evals/tests/` (which scores `alias_golden_v1.yaml` and `task_context_ab_v1.yaml` against
hand-fed or fixture-seeded inputs — `test_alias.py`, `test_golden.py`, `test_golden_gate.py`,
`test_task_context_ab.py`, `test_cases_load.py`, among others).

**Why it exists:** every expected answer in these sets — expected docs/files/symbols/tests,
expected top-1 alias targets, expected `get_task_context` file coverage — was **hand-written before
any run** (docs/contracts/evals-report.md, `alias_golden_v1.yaml`'s `provenance` field,
`task_context_ab_v1.yaml`'s header comment). This is the platform's "no grading your own homework"
floor: golden expectations pinned in git, scored deterministically, zero LLM calls.

**Where it runs / what it needs:** a **migrated but not-necessarily-built** registry
(`TEST_DATABASE_URL`) — the harness seeds its own tiny synthetic fixtures into it (see
`harness/fixtures.py`, `harness/task_context_ab.py::seed_ab_case`). It does not need a real
connector build. `make migrate-test-db` is the only setup step.

**A note on internal overlap, by design:** `evals/tests/` mixes true T1 golden-set scoring with
ordinary harness-unit tests (`test_metrics.py`, `test_baseline.py`, `test_report.py`,
`test_run_status.py` pin the *math*, not a golden expectation). Splitting the suite file-by-file so
`run_all.py` could invoke only the "pure T1" subset would require the runner to hard-code internal
test-file knowledge that will rot. The whole suite runs in under two seconds and needs no
credentials, so treating it as one T1 check is a deliberate simplification, not an oversight — and
it deliberately overlaps with T0's `verify-evals` when `--with-gates` is also on. That duplication
is accepted: T0 answers "is the harness's own code correct," T1 answers "do the golden sets pass,"
and the overlap costs nothing (the tests are fast).

### T2 — Live-KB deterministic

**What:** two zero-LLM checks against a **really built** local KB registry:

1. **Alias full run** (`scripts/eval_alias_resolution.py`) — resolves all 25
   `alias_golden_v1.yaml` cases against the live `alias_reference` index and reports top-1 accuracy
   (target ≥ 80%; see `docs/reports/alias-accuracy-2026-07-03.md` for the last real run: 25/25).
2. **`get_task_context` latency probe** (`harness/task_context_latency.py`, new in this
   consolidation) — calls the real `get_task_context` tool in-process, zero LLM, once per task
   string reused from `agent_task_cases/task_context_ab_v1.yaml` (DRY — no new golden set), against
   whatever `DATABASE_URL` points at, and reports p50/p95 wall-clock latency plus any errors.

**Why it exists, and why it's separate from T1:** T1 proves the deterministic pipelines are
*correct* against a small, hermetic, harness-seeded world. T2 proves the same pipelines still work
— and stay fast — against **real content** from an actual connector build: real commit history,
real code symbols, a real graph shape T1's tiny fixtures can't reproduce. This is the same
distinction `docs/reports/alias-accuracy-2026-07-03.md` draws explicitly in its own caveats
section ("this is a fit-to-source check … a production KB would very likely score lower").

**Why the latency probe, specifically:** PR-39's acceptance criteria required `get_task_context`'s
p50 to be "measured and printed." That is satisfied *hermetically*, against seeded fixtures, by
`services/mcp-server/tests/integration/test_task_context.py::test_p50_on_a_seeded_kb_is_measured_and_printed`
(part of T0). What no fixture can prove is that the tool stays fast and error-free against a real
graph (3,297 alias artifacts / 8,898+ edges in the KB this was verified against, not a dozen
hand-written fixture rows) — that is what T2's probe adds, and it is a genuinely new, small piece
of this consolidation (see §4).

**Where it runs / what it needs:** `DATABASE_URL` pointing at a locally built KB (see
`docs/dev-guide/22-testing-and-builds.md` §5 "Run a local build"). Both checks are
**read-only** with respect to KB content: the alias script only executes `SELECT`s
(`harness.alias`/`agentic_kb_builder.alias.resolve` never write); the latency probe calls the real
broker tool, which writes exactly one `retrieval_event` ledger row per call (the broker's only
Postgres write, `infrastructure/postgres/retrieval_events.py`) — ordinary telemetry, not a KB
content mutation, and the same side effect any real call to the tool has.

### T3 — LLM-armed two-arm A/B

**What:** `scripts/eval_task_context.py`'s two-arm comparison — **tooled** (`get_task_context`
offered) vs. **raw** (file tools only) — over `agent_task_cases/task_context_ab_v1.yaml`'s ten
realistic dev tasks, run through the same model via `scripts/kb_agent.py`'s minimal
provider-agnostic shim (`LLM_PROVIDER` ∈ groq / openai / anthropic\_foundry / anthropic).

**Why it exists:** T1 and T2 prove the tool returns the *right* files. T3 is the only tier that
asks whether an actual agent, given the tool, *uses* it well enough to change what it does —
coverage, steps, tokens, per-arm — against the alternative of no tool at all.

**Results are per-task-type, and flakes are flagged, not hidden.** The house precedent
(`docs/reports/task-context-ab-2026-07-03.md`) reports coverage broken out by task kind
("history-echoing maintenance" vs. "novel/prospective work") because the aggregate mean hides where
the tool actually earns its keep, and separately counts `ERR@0` model flakes (a hallucinated tool
name → provider 400) so they are never silently folded into "0.00 coverage" as if the tool failed —
`harness.tier_parsers.parse_task_context_ab_output` carries this same discipline into the
consolidated report's `flakes` metric.

**Where it runs / what it needs:** `DATABASE_URL` (a real built KB) **and** LLM credentials
(`LLM_API_KEY` or `GROQ_API_KEY`, per `scripts/kb_agent.py`'s resolution order). It runs from
`services/mcp-server` (the script's own docstring: it needs that project's dependencies to call
the broker in-process). Because it spends real tokens, the consolidated runner defaults to a
**bounded 3-case smoke** (`--t3-limit 3`); `--t3-full` runs all ten. This is the tier the platform
owner runs deliberately after a change lands — the consolidated runner never triggers it in an
environment without both preconditions, and this consolidation does not re-run it (see §2 forbidden
loops: T3 is a measurement, not a target to iterate against).

### T4 — Adversarial fixtures

**What:** ≥ 5 prompt-injection payloads (`services/review-panel/tests/integration/test_injection.py`);
budget-cap enforcement (`services/mcp-server/tests/unit/test_budgets.py`,
`test_kb_search_budget.py`, `test_token_budget.py`); ACL/team-scoping
(`services/mcp-server/tests/unit/test_rbac.py`, `tests/integration/test_security.py`); and the
BUILD-lane dev-gate assertions (`services/mcp-server/tests/unit/test_runner_build_lane.py`,
ADR-0031).

**Why these live in the owning service's suite, not in `evals/`:** each fixture needs its service's
*real* dependencies to be a meaningful test — review-panel's injection suite needs the actual
LangGraph checkpointer and the actual untrusted-content fencing (`review_panel.domain.untrusted`);
mcp-server's ACL/budget suites need the actual `rbac.py`/`budgets.py` enforcement code, not a
re-implementation. Hoisting them into `evals/` (a dev-only project with a *path dependency* on
mcp-server, per ADR-0008) would mean either duplicating that machinery — a DRY and
self-contained-services violation — or importing test-support internals `evals/` explicitly does
not import today (`harness/fixtures.py`'s own docstring: raw-SQL seed helpers are duplicated from
mcp-server's test support *because* evals doesn't import `tests/`). These are correctness/security
assertions about product code, the same category as T0, not a retrieval-quality evaluation; they
belong with the code they protect.

**Where it runs:** as an ordinary part of each service's own `pytest` — i.e. inside T0
(`make verify`, or `make test-mcp-server` / `make test-review-panel` directly). The consolidated
runner does not execute T4 itself; it reports a static inventory (file list, one line each) and
points at T0. Marking T4 `skip` in the consolidated report is not "unavailable" in the T1/T2/T3
sense (a missing credential) — it is architecturally intentional, and the skip reason says so
explicitly rather than reading as an apology.

## 4. The consolidated runner (`evals/run_all.py`)

### Design decisions

- **One entry point, thin orchestration.** `run_all.py` never reimplements retrieval or
  agent-quality logic. Each `harness.tiers.run_tN` function shells out to (T0/T1/T3), or calls
  in-process (T2's latency probe, T4), the runner/suite that already owns that tier, and translates
  the result into a `TierResult`/`CheckResult` (`harness/tier_result.py`).
- **T0 is a summary of `make verify`, made opt-in.** The brief considered keeping T0 in scope by
  default; in practice it duplicates CI and re-syncs three services' venvs on every run, so it is
  gated behind `--with-gates`. This was judged the cleanest split: `run_all.py` without flags is a
  fast, always-safe "what can I learn about retrieval/agent quality right now" command; adding
  `--with-gates` turns it into the full platform gate.
  `run.py` (T1's own runner) is intentionally **left untouched** — it is still the tool
  `docs/contracts/evals-report.md` and the `eval-runner` subagent read directly; `run_all.py`
  is a layer above it, not a replacement.
- **Every I/O seam is injectable, for testability.** Every `run_tN` function takes explicit
  `database_url`/`env`/`runner` parameters (`runner` defaults to `subprocess.run`; the others have
  no environment fallback at all — next bullet). This is what makes skip-with-reason behavior and
  output-parsing logic testable without a real database, subprocess, or LLM call
  (`evals/tests/test_tiers.py`).
- **Environment detection happens exactly once, in `run_all.run()`; tier functions never read
  `os.environ`.** There is no `--database-url` flag: the runner reads `TEST_DATABASE_URL` (T1),
  `DATABASE_URL` (T2/T3), and `LLM_API_KEY`/`GROQ_API_KEY` (T3) at the top and passes explicit
  values down. This split is load-bearing, learned from a real incident: the first version let a
  tier function fall back to `os.environ` when its parameter was `None`, so "None = absent" (what
  the skip tests meant) and "None = read the env" (what the code did) diverged — and since T1
  spawns evals' own pytest suite with `TEST_DATABASE_URL` injected, a skip-behavior test inside
  that child executed T1 for real and spawned pytest again: a fork bomb (observed 2026-07-05
  during this feature's own verification, one new pytest generation every ~6 s until killed).
  Two guarantees now pin the fix (`test_tiers.py`, "ambient environment" section): tier functions
  are pure with respect to the environment, and T1's pytest child carries
  `EVAL_RUN_ALL_INNER=1` (`harness.tiers.INNER_PYTEST_GUARD`), under which
  `tests/test_run_all.py` skips itself — the outer `run_all` execution is the coverage for what
  those tests pin, so even a future regression cannot recurse.
- **T1 wants an exclusive registry.** `run.py` and the DB-backed evals tests seed and DELETE the
  registry they point at; the shared local `agentic_kb_test` is also used by the services' own
  test suites (`run.py`'s comment: "the registry is shared with the services' test suites").
  Running T1 while a sibling suite writes the same registry can fail cleanup with an FK violation
  (observed once during verification: a concurrent commit landing between cleanup's
  `DELETE FROM knowledge_artifact` and `DELETE FROM source_item`). When sibling workstreams are
  active, point `TEST_DATABASE_URL` at a dedicated migrated database (`createdb` + kb-builder's
  `alembic upgrade head`) instead of the shared one.
- **Parsing is separate from execution.** `harness/tier_parsers.py` holds the pure
  stdout/JSON-parsing functions (`parse_alias_output`, `parse_task_context_ab_output`,
  `parse_run_py_report`); `harness/tiers.py` holds the subprocess/DB glue. The parsing logic is the
  part most likely to need a regex tweak when a script's print format changes, so it is isolated
  and directly unit-tested against captured strings.

### CLI

```sh
cd evals && uv run python run_all.py                # T1-T4 (T1/T2/T3 auto-skip if unavailable)
cd evals && uv run python run_all.py --with-gates    # + T0 (make verify)
cd evals && uv run python run_all.py --tiers t1,t2   # only these tiers
cd evals && uv run python run_all.py --t3-full       # all 10 T3 cases, not the default 3-case smoke
cd evals && uv run python run_all.py --out report.md # markdown to stdout AND this path (default: evals/report_all.md)
```

Or via the Makefile: `make eval-all` (wraps the same invocation with `TEST_DATABASE_URL`; add
`--with-gates`/`--tiers` by invoking `run_all.py` directly for anything beyond the default).

### Report shape

One table (tier, status, duration, check count), then one section per tier with its checks
(name, status, reason, metrics, and — on failure — the verbatim `detail`), then an overall
pass/fail/skip line. Exit code is `0` unless some tier's status is `fail` — a fully-skipped run
(nothing configured in this shell) is a valid, honest `0`.

## 5. How to add a case, per tier

- **T0:** nothing to add here — it is generic per-project correctness. Add a unit/integration test
  to the relevant service as you would for any bug fix or feature.
- **T1:** add a case YAML under `evals/retrieval_cases/` or `evals/agent_task_cases/`
  (`harness/cases.py`'s `EvalCase` schema — expected docs/files/symbols/tests/open_questions,
  written before you run anything against it), or a golden case under
  `evals/retrieval_cases/golden/` (`harness/golden.py`'s `GoldenCase`). Run `uv run pytest` and
  `TEST_DATABASE_URL=... uv run python run.py` locally before committing.
- **T2:** add a case to `evals/retrieval_cases/alias_golden_v1.yaml` (`harness/alias.py`'s
  `AliasCase` — a hand-verified query + expected target path(s), with a `provenance` field showing
  how you verified it) for the alias check. The latency probe reuses T3's task list — add a task
  there instead (see below) if you want it covered.
- **T3:** add a case to `evals/agent_task_cases/task_context_ab_v1.yaml`
  (`harness.task_context_ab.TaskContextAbCase` — a realistic task, hand-written `expected_files`,
  and a small fixture KB). This case is scored twice: hermetically by
  `evals/tests/test_task_context_ab.py` (T1) and live by `scripts/eval_task_context.py` (T3) —
  writing it once covers both.
- **T4:** add the fixture to the owning service's own test suite (review-panel's
  `tests/integration/test_injection.py` for a new injection payload; mcp-server's
  `tests/unit/test_budgets.py`/`test_rbac.py` for a new budget/ACL case) using that suite's existing
  helpers. Do not add it under `evals/` — see §3's T4 rationale.

In every case: **write the expected answer first**, before running anything against it. A case
whose "expected" values were derived by looking at what the system already returns is not a golden
case — see §2's forbidden loops.

## 6. Where results go

Two different things are called "a report" here, and they serve different audiences:

- **The consolidated runner's markdown** (`evals/run_all.py --out <path>`, default
  `evals/report_all.md`) is a **machine-generated status snapshot**: what ran, pass/fail/skip,
  duration, key metrics, verbatim failure detail. It is meant to be regenerated on every run and is
  not itself committed or curated — it is the input a human or the `eval-runner` subagent reads to
  decide what to look into next.
- **`docs/reports/<topic>-<date>.md`** (e.g. `kb-benefit-2026-06-18.md`,
  `alias-accuracy-2026-07-03.md`, `task-context-ab-2026-07-03.md`) is a **hand-curated, dated
  write-up** of one real run, in the house honest-reporting style: setup, results, findings,
  caveats, bottom line — written by a human (or an agent on the owner's behalf) after inspecting a
  real run's output, never auto-generated wholesale. A noteworthy consolidated-run finding graduates
  into one of these when it's worth a permanent, narrated record; routine runs do not.

Do not confuse the two: the consolidated report is disposable and reproducible on demand; a
`docs/reports/*.md` entry is a permanent, narrated citation of one specific run and is never
overwritten by a later run with different numbers (a new dated file is added instead).

## 7. Environment reference

| Variable | Gates | Example (local) |
|---|---|---|
| `TEST_DATABASE_URL` | T1 | `postgresql+asyncpg://$USER@localhost:5432/agentic_kb_test` (`make migrate-test-db`) |
| `DATABASE_URL` | T2, T3 | `postgresql+asyncpg://$USER@localhost:5432/<a locally built KB>` |
| `LLM_API_KEY` / `GROQ_API_KEY` | T3 | see `scripts/kb_agent.py`'s module docstring for the full provider matrix |

No variable is required to run `evals/run_all.py` itself — every tier it can't satisfy skips with
the reason above, and the run still exits `0`.

## 8. Diagrams

Two sequence diagrams in this directory illustrate flows this document scores: `seq-task-flow.mmd`
(the `get_task_context` resolve → blast-radius → conventions → similar-changes → synthesize path
T2's latency probe and T3's A/B harness exercise) and `seq-review-flow.mmd` (the review-panel
fan-out/reconcile/draft path T4's injection and dev-gate fixtures protect).
`e2e-flow-detailed.mmd` is the whole-pipeline diagram (build → registry → MCP tools → host
surfaces → review draft engine); it is broader than this document's scope and is the natural
companion to `00-overview.md`.
