"""Operator replay CLI: `python -m agentic_mcp_server.replay <run_id>`.

Loads all retrieval_event rows for the given run_id in time order and prints a
human-readable action timeline so an operator can verify exactly what agent X
did for request Y — retrieval, trust, budget, citations, and gate decisions.

Format (one line per action):

  HH:MM:SS  +elapsed  TOOL  headline :: key=value ...

The ``details`` JSONB column is expanded inline. No authentication required —
this is an operator tool that connects directly to DATABASE_URL.

Exit codes: 0 = found rows and printed them, 1 = no rows found, 2 = bad args.
"""

import asyncio
import os
import sys
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from agentic_mcp_server.infrastructure.postgres.retrieval_events import (
    ReplayEventRow,
    replay_events,
)


def _fmt_time(dt: datetime) -> str:
    local = dt.astimezone()
    return local.strftime("%H:%M:%S")


def _fmt_elapsed(delta_ms: float) -> str:
    if delta_ms < 1000:
        return f"+{delta_ms:.0f}ms"
    return f"+{delta_ms / 1000:.1f}s"


def _expand_details(tool_name: str, details: dict[str, Any] | None) -> str:
    """Render the ``details`` JSONB as a human-readable key=value suffix."""
    if not details:
        return ""

    parts: list[str] = []

    if tool_name == "context.create_pack":
        task = details.get("task", "")
        budget = details.get("budget", {})
        cards = details.get("cards", [])
        candidates = details.get("candidates_considered", 0)
        parts.append(f"task={task!r}")
        parts.append(f"candidates={candidates}")
        parts.append(f"cards={len(cards)}")
        if budget:
            parts.append(f"budget_used={budget.get('used', '?')}/{budget.get('allowed', '?')}")
        if cards:
            card_titles = ", ".join(
                f"{c.get('title', '?')} ({c.get('score', 0):.2f})" for c in cards[:3]
            )
            if len(cards) > 3:
                card_titles += f" +{len(cards) - 3} more"
            parts.append(f"top_cards=[{card_titles}]")

    elif tool_name == "context.expand":
        seeds = details.get("seed_artifact_ids", [])
        tiers = details.get("tiers", [])
        parts.append(f"seeds={len(seeds)}")
        parts.append(f"tiers={'+'.join(tiers)}")
        parts.append(f"cards_added={details.get('cards_added', 0)}")
        parts.append(f"tokens={details.get('tokens', 0)}")
        if details.get("truncated"):
            parts.append("truncated=true")

    elif tool_name == "context.open_evidence":
        parts.append(f"evidence_id={details.get('evidence_id', '?')}")
        parts.append(f"level={details.get('level', 'L2')}")
        parts.append(f"tokens={details.get('tokens', 0)}")
        if details.get("injection_flagged"):
            parts.append("injection_flagged=true")

    elif tool_name == "graph.get_neighbors":
        nbt = details.get("neighbors_by_type", {})
        nbt_str = " ".join(f"{k}:{v}" for k, v in nbt.items()) or "none"
        parts.append(f"artifact={details.get('artifact_id', '?')[:8]}...")
        parts.append(f"depth={details.get('depth', '?')}")
        parts.append(f"trust_floor={details.get('trust_floor', '?')}")
        parts.append(f"by_type=[{nbt_str}]")

    elif tool_name == "context.verify_answer":
        claims = details.get("claims", [])
        overall = details.get("overall", "?")
        passed = sum(1 for c in claims if c.get("ok"))
        parts.append(f"answer={details.get('answer_id', '?')}")
        parts.append(f"claims={len(claims)}")
        parts.append(f"passed={passed}")
        parts.append(f"overall={overall}")

    elif tool_name == "governance.checkpoint":
        parts.append(f"from={details.get('from_agent', '?')}")
        parts.append(f"to={details.get('to_agent', '?')}")
        parts.append(f"decision={details.get('decision', '?')}")
        edits = details.get("edits", [])
        if edits:
            parts.append(f"edits={len(edits)}")
        plan = details.get("plan_summary", "")
        if plan:
            short = plan[:60] + ("..." if len(plan) > 60 else "")
            parts.append(f"plan={short!r}")

    else:
        # Unknown tool: dump key=value for every top-level key (skip nested).
        for k, v in details.items():
            if not isinstance(v, (dict, list)):
                parts.append(f"{k}={v!r}")

    return " :: " + "  ".join(parts) if parts else ""


def _headline(row: ReplayEventRow) -> str:
    """One-line summary for a row (tool name + status + token count)."""
    base = f"{row.tool_name}  [{row.status}]"
    if row.tokens_returned:
        base += f"  {row.tokens_returned}tok"
    if row.latency_ms is not None:
        base += f"  {row.latency_ms}ms"
    return base


def render_timeline(rows: list[ReplayEventRow]) -> list[str]:
    """Render a list of replay rows as timeline lines (pure, testable).

    Returns one string per row. The first row is the epoch; subsequent rows
    show elapsed time since the first.
    """
    if not rows:
        return []

    lines: list[str] = []
    epoch = rows[0].created_at
    # Ensure timezone-aware comparison.
    if epoch.tzinfo is None:
        epoch = epoch.replace(tzinfo=UTC)

    for row in rows:
        ts = row.created_at
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        delta_ms = (ts - epoch).total_seconds() * 1000
        time_str = _fmt_time(ts)
        elapsed_str = _fmt_elapsed(delta_ms)
        headline = _headline(row)
        detail_str = _expand_details(row.tool_name, row.details)
        line = f"{time_str}  {elapsed_str:>8}  {headline}{detail_str}"
        lines.append(line)

    return lines


async def _run(run_id: str) -> int:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("ERROR: DATABASE_URL is not set", file=sys.stderr)
        return 2

    engine = create_async_engine(database_url)
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)

    async with factory() as session:
        rows = await replay_events(session, run_id)

    await engine.dispose()

    if not rows:
        print(f"No events found for run_id={run_id!r}", file=sys.stderr)
        return 1

    print(f"Action timeline for run {run_id!r}  ({len(rows)} events)")
    print("-" * 72)
    for line in render_timeline(rows):
        print(line)
    return 0


def main() -> None:
    if len(sys.argv) != 2:
        print("Usage: python -m agentic_mcp_server.replay <run_id>", file=sys.stderr)
        sys.exit(2)
    run_id = sys.argv[1]
    sys.exit(asyncio.run(_run(run_id)))


if __name__ == "__main__":
    main()
