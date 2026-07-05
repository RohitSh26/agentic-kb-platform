"""get_task_context latency probe against a REAL built KB (T2 — zero fixtures, zero LLM).

Times the real broker tool (`agentic_mcp_server.context_broker.task_context.get_task_context`)
in-process over realistic task strings, against whatever registry `DATABASE_URL` points at. No
ground truth is asserted here — there is no golden set for an arbitrary live KB's content — this
is a smoke + latency signal, the T2 counterpart to PR-39's acceptance criterion ("p50 measured and
printed"). That criterion is otherwise satisfied hermetically, with SEEDED fixtures, by
mcp-server's own perf test (`tests/integration/test_task_context.py`,
`test_p50_on_a_seeded_kb_is_measured_and_printed`, part of T0). Running the SAME tool against
REAL content — not fixtures — is the thing only a built KB can prove: that it stays fast and
doesn't error on a real graph, not just a synthetic one.

Task strings are reused from `agent_task_cases/task_context_ab_v1.yaml` (already hand-written,
realistic dev tasks — DRY, not a new golden set) via `harness.task_context_ab.load_ab_cases`.
Each call also writes one `retrieval_event` ledger row — the broker's only Postgres write (see
`infrastructure/postgres/retrieval_events.py`) — the same, expected side effect any real
`get_task_context` call has; nothing else in the registry is touched.
"""

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class LatencyResult:
    task: str
    seconds: float | None  # None on error
    error: str | None = None


@dataclass(frozen=True)
class LatencyReport:
    n: int
    p50_seconds: float | None
    p95_seconds: float | None
    errors: tuple[str, ...]  # task strings that errored, verbatim reasons live in LatencyResult


def _percentile(sorted_values: Sequence[float], pct: float) -> float:
    if len(sorted_values) == 1:
        return sorted_values[0]
    index = min(len(sorted_values) - 1, round(pct * (len(sorted_values) - 1)))
    return sorted_values[index]


def summarize(results: Sequence[LatencyResult]) -> LatencyReport:
    ok = sorted(
        result.seconds for result in results if result.error is None and result.seconds is not None
    )
    errors = tuple(result.task for result in results if result.error is not None)
    if not ok:
        return LatencyReport(n=len(results), p50_seconds=None, p95_seconds=None, errors=errors)
    return LatencyReport(
        n=len(results),
        p50_seconds=_percentile(ok, 0.50),
        p95_seconds=_percentile(ok, 0.95),
        errors=errors,
    )


async def probe(database_url: str, tasks: Sequence[str]) -> list[LatencyResult]:
    """Call the real tool once per task string, in-process, against `database_url`."""
    import time

    from agentic_mcp_server.auth.rbac import Requester
    from agentic_mcp_server.context_broker.dependencies import BrokerDeps
    from agentic_mcp_server.context_broker.task_context import get_task_context
    from agentic_mcp_server.infrastructure.postgres.keyword_search import (
        PostgresKeywordSearchClient,
    )
    from agentic_mcp_server.mcp.tool_schemas.task_context import GetTaskContextRequest
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(database_url)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        deps = BrokerDeps(
            session_factory=factory, search_client=PostgresKeywordSearchClient(factory)
        )
        requester = Requester(subject="eval-t2-latency", teams=frozenset())
        results: list[LatencyResult] = []
        for task in tasks:
            start = time.monotonic()
            try:
                await get_task_context(
                    deps, GetTaskContextRequest(task_description=task), requester
                )
                results.append(LatencyResult(task=task, seconds=time.monotonic() - start))
            except Exception as exc:  # a bad/real-world case must not kill the whole probe
                results.append(
                    LatencyResult(task=task, seconds=None, error=f"{type(exc).__name__}: {exc}")
                )
        return results
    finally:
        await engine.dispose()
