# Evals

Build the benchmark before expanding autonomy (see docs/architecture §13 and
docs/contracts/evals-report.md). Each case lists expected docs, files, symbols, tests, and open
questions; `run.py` executes them through the Context Broker, computes the §13 metrics, and diffs
against `baseline.json`. The `eval-runner` build subagent executes the harness and reads its
stdout table + verdict.

```sh
cd evals && uv sync
# needs a migrated local registry (kb-builder owns migrations):
make -C .. migrate-test-db TEST_DATABASE_URL=postgresql+asyncpg://$USER@localhost:5432/agentic_kb_test
TEST_DATABASE_URL=postgresql+asyncpg://$USER@localhost:5432/agentic_kb_test uv run python run.py
uv run python run.py --update-baseline   # rewrite the committed baseline
```

`evals/` is a dev-only uv project with a path dependency on `services/mcp-server` — it is never
deployed and never carries Alembic. Local runs never require Azure (`FakeSearchClient` + Postgres).
Do NOT modify product code to make a case pass; file real defects instead.

- `retrieval_cases/` — single-query recall checks against seeded fixtures.
- `agent_task_cases/` — the six §13 benchmark task types: scripted broker-call sequences plus a
  scripted agent output whose claims are validated against the evidence IDs actually returned.
- `harness/` — case loader, fixture seeding, executor, pure metric computation, baseline diffing.
