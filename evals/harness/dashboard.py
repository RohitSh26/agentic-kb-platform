"""Read-only operator dashboard (ADR-0014 Phase 1, docs/contracts/observability-dashboard.md).

Renders a static HTML + Markdown report from the four dashboard views
(v_retrieval_health, v_token_economics, v_build_health, v_budget_adherence)
plus, when evals/report.json exists, the latest golden publish-gate block. The
renderer is aggregate-only by construction: every query below names its columns
explicitly and never touches query_text / normalized_query / body_text
(statically asserted in tests/test_dashboard.py). It issues SELECTs only —
never a write — so pointing it at a real registry via DATABASE_URL is safe.
"""

import json
import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from html import escape
from pathlib import Path
from typing import Any, cast

from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from harness.golden import DEFAULT_MIN_EVIDENCE_RECALL

logger = logging.getLogger(__name__)

# Column names shared with the eval gates carry the pinned definitions (ADR-0014's
# hard rule: a gate and its dashboard tile can never disagree on a definition).
# tests/test_dashboard.py asserts membership in harness.metrics.METRIC_NAMES.
LEDGER_METRIC_NAMES: tuple[str, ...] = (
    "evidence_reuse_rate",
    "semantic_cache_hit_rate",
    "retrieval_calls_per_agent",
)

RECENT_DAYS = 14
SUMMARY_DAYS = 7
BUILD_LIMIT = 20
BREACH_LIMIT = 50

SQL_RETRIEVAL_HEALTH = """
SELECT day, events, approved, reused, denied, needs_human_approval, errors,
       error_rate, evidence_reuse_rate, semantic_cache_hit_rate, cache_hit_rate,
       kb_search_answered, kb_search_zero_thin, kb_search_zero_thin_rate
FROM v_retrieval_health
ORDER BY day DESC
LIMIT :days
"""

SQL_TOKEN_ECONOMICS = """
SELECT day, runs, agents, events, tokens_charged, context_tokens_per_run,
       retrieval_calls_per_agent
FROM v_token_economics
ORDER BY day DESC
LIMIT :days
"""

SQL_BUILD_HEALTH = """
SELECT kb_version, build_seq, status, started_at, completed_at, duration_seconds,
       sources_seen, sources_changed, artifacts_created, artifacts_updated,
       artifacts_deleted, llm_calls, embedding_calls, llm_calls_per_changed_source,
       extractor_failures, failed_gate, gate_measured_value, is_active,
       active_kb_age_seconds
FROM v_build_health
ORDER BY build_seq DESC
LIMIT :limit
"""

SQL_BUDGET_BREACHES = """
SELECT run_id, agent_name, events, tokens_charged, run_tokens, run_budget_tokens,
       over_run_budget, follow_up_requests, follow_up_tokens, agent_max_requests,
       agent_max_tokens, over_agent_requests, over_agent_tokens
FROM v_budget_adherence
WHERE over_run_budget OR over_agent_requests OR over_agent_tokens
ORDER BY run_tokens DESC
LIMIT :limit
"""

SQL_BUDGET_TOTALS = """
SELECT count(*) AS pairs,
       count(DISTINCT run_id) AS runs,
       count(DISTINCT run_id) FILTER (WHERE over_run_budget) AS runs_over_budget,
       count(*) FILTER (WHERE over_agent_requests OR over_agent_tokens)
           AS agents_over_allowance
FROM v_budget_adherence
"""

ALL_SQL: tuple[str, ...] = (
    SQL_RETRIEVAL_HEALTH,
    SQL_TOKEN_ECONOMICS,
    SQL_BUILD_HEALTH,
    SQL_BUDGET_BREACHES,
    SQL_BUDGET_TOTALS,
)

Row = dict[str, Any]


@dataclass(frozen=True)
class DashboardData:
    generated_at: datetime
    database: str  # credentials redacted
    retrieval_health: list[Row]
    token_economics: list[Row]
    build_health: list[Row]
    budget_breaches: list[Row]
    budget_totals: Row
    golden: Row | None  # latest report.json golden block, when present


@dataclass(frozen=True)
class Tile:
    """One at-a-glance answer: label, value, ok|warn|bad status class."""

    label: str
    value: str
    status: str


def _load_golden_block(report_path: Path | None) -> Row | None:
    if report_path is None or not report_path.exists():
        return None
    try:
        parsed: object = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        logger.warning("dashboard.golden_block_unreadable path=%s error=%s", report_path, error)
        return None
    if not isinstance(parsed, dict):
        return None
    report = cast(Row, parsed)
    golden_raw = report.get("golden")
    if not isinstance(golden_raw, dict):
        return None
    golden: Row = dict(cast(Row, golden_raw))
    golden["report_created_at"] = report.get("created_at")
    golden["report_git_sha"] = report.get("git_sha")
    return golden


async def fetch_dashboard(database_url: str, *, report_path: Path | None = None) -> DashboardData:
    engine = create_async_engine(database_url)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:

            async def rows(sql: str, params: dict[str, int]) -> list[Row]:
                result = await session.execute(text(sql), params)
                return [dict(mapping) for mapping in result.mappings().all()]

            retrieval_health = await rows(SQL_RETRIEVAL_HEALTH, {"days": RECENT_DAYS})
            token_economics = await rows(SQL_TOKEN_ECONOMICS, {"days": RECENT_DAYS})
            build_health = await rows(SQL_BUILD_HEALTH, {"limit": BUILD_LIMIT})
            budget_breaches = await rows(SQL_BUDGET_BREACHES, {"limit": BREACH_LIMIT})
            budget_totals = (await rows(SQL_BUDGET_TOTALS, {}))[0]
    finally:
        await engine.dispose()
    return DashboardData(
        generated_at=datetime.now(UTC),
        database=make_url(database_url).render_as_string(hide_password=True),
        retrieval_health=retrieval_health,
        token_economics=token_economics,
        build_health=build_health,
        budget_breaches=budget_breaches,
        budget_totals=budget_totals,
        golden=_load_golden_block(report_path),
    )


# --- formatting ------------------------------------------------------------------


def _fmt_rate(value: Any) -> str:
    return "n/a" if value is None else f"{float(value):.1%}"


def _fmt_num(value: Any) -> str:
    if value is None:
        return "n/a"
    number = float(value)
    return f"{number:,.0f}" if number == int(number) else f"{number:,.1f}"


def _fmt_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else ""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    if isinstance(value, date):
        return value.isoformat()
    # numeric(...) columns (extract(epoch ...)) arrive as Decimal via asyncpg
    if isinstance(value, int | float | Decimal):
        return _fmt_num(value)
    return str(value)


def _fmt_hours(seconds: Any) -> str:
    return "n/a" if seconds is None else f"{float(seconds) / 3600:.1f}h"


def _sum(rows: list[Row], column: str) -> int:
    return sum(int(row[column] or 0) for row in rows)


def _rate(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


# --- summary tiles ---------------------------------------------------------------


def _rate_status(rate: float | None, *, warn: float, bad: float) -> str:
    if rate is None:
        return "ok"
    if rate >= bad:
        return "bad"
    return "warn" if rate >= warn else "ok"


def _build_tiles(data: DashboardData) -> list[Tile]:
    recent = data.retrieval_health[:SUMMARY_DAYS]
    events = _sum(recent, "events")
    error_rate = _rate(_sum(recent, "errors"), events)
    reuse_eligible = _sum(recent, "reused") + _sum(recent, "approved")
    reuse_rate = _rate(_sum(recent, "reused"), reuse_eligible)
    gap_rate = _rate(_sum(recent, "kb_search_zero_thin"), _sum(recent, "kb_search_answered"))
    week_tokens = _sum(data.token_economics[:SUMMARY_DAYS], "tokens_charged")

    tiles = [
        Tile(
            label=f"Retrieval events ({SUMMARY_DAYS}d)",
            value=_fmt_num(events),
            status="ok" if events else "warn",
        ),
        Tile(
            label=f"Error rate ({SUMMARY_DAYS}d)",
            value=_fmt_rate(error_rate),
            status=_rate_status(error_rate, warn=0.01, bad=0.05),
        ),
        Tile(
            label=f"Evidence reuse rate ({SUMMARY_DAYS}d)",
            value=_fmt_rate(reuse_rate),
            status="ok",
        ),
        Tile(
            label=f"KB-gap proxy: kb_search zero/thin ({SUMMARY_DAYS}d)",
            value=_fmt_rate(gap_rate),
            status=_rate_status(gap_rate, warn=0.2, bad=0.5),
        ),
        Tile(label=f"Tokens charged ({SUMMARY_DAYS}d)", value=_fmt_num(week_tokens), status="ok"),
    ]

    runs_over = int(data.budget_totals["runs_over_budget"] or 0)
    agents_over = int(data.budget_totals["agents_over_allowance"] or 0)
    tiles.append(
        Tile(
            label="Budget breaches (runs over / agents over)",
            value=f"{runs_over} / {agents_over}",
            status="ok" if runs_over + agents_over == 0 else "bad",
        )
    )

    latest = data.build_health[0] if data.build_health else None
    if latest is None:
        tiles.append(Tile(label="Latest build", value="no builds recorded", status="warn"))
    else:
        gate = f" (gate: {latest['failed_gate']})" if latest["failed_gate"] else ""
        good = latest["status"] in ("active", "completed")
        tiles.append(
            Tile(
                label=f"Latest build ({latest['kb_version']})",
                value=f"{latest['status']}{gate}",
                status="ok" if good else "bad",
            )
        )
    active = next((row for row in data.build_health if row["is_active"]), None)
    tiles.append(
        Tile(
            label="Active KB age",
            value=_fmt_hours(active["active_kb_age_seconds"]) if active else "no active build",
            status="ok" if active else "bad",
        )
    )

    if data.golden is not None:
        mean = data.golden.get("mean_evidence_recall")
        leaks = int(data.golden.get("total_acl_leaks") or 0)
        passed = mean is not None and float(mean) >= DEFAULT_MIN_EVIDENCE_RECALL and leaks == 0
        tiles.append(
            Tile(
                label=f"Golden gate (floor {DEFAULT_MIN_EVIDENCE_RECALL}, latest eval run)",
                value=f"mean recall {_fmt_rate(mean)}, acl_leaks {leaks}",
                status="ok" if passed else "bad",
            )
        )
    return tiles


# --- section tables (shared row shaping for MD + HTML) ----------------------------


@dataclass(frozen=True)
class Section:
    title: str
    headers: list[str]
    rows: list[list[str]]
    empty_note: str


def _section(title: str, columns: list[str], rows: list[Row], empty_note: str) -> Section:
    rate_columns = {
        "error_rate",
        "evidence_reuse_rate",
        "semantic_cache_hit_rate",
        "cache_hit_rate",
        "kb_search_zero_thin_rate",
    }

    def cell(row: Row, col: str) -> str:
        if col in rate_columns and row[col] is not None:
            return _fmt_rate(row[col])
        return _fmt_cell(row[col])

    shaped = [[cell(row, col) for col in columns] for row in rows]
    return Section(title=title, headers=columns, rows=shaped, empty_note=empty_note)


def _sections(data: DashboardData) -> list[Section]:
    return [
        _section(
            f"Retrieval health (last {RECENT_DAYS} days)",
            [
                "day",
                "events",
                "approved",
                "reused",
                "denied",
                "errors",
                "error_rate",
                "evidence_reuse_rate",
                "semantic_cache_hit_rate",
                "cache_hit_rate",
                "kb_search_answered",
                "kb_search_zero_thin",
                "kb_search_zero_thin_rate",
            ],
            data.retrieval_health,
            "no retrieval events recorded",
        ),
        _section(
            f"Token economics (last {RECENT_DAYS} days)",
            [
                "day",
                "runs",
                "agents",
                "events",
                "tokens_charged",
                "context_tokens_per_run",
                "retrieval_calls_per_agent",
            ],
            data.token_economics,
            "no retrieval events recorded",
        ),
        _section(
            f"Build health (last {BUILD_LIMIT} builds)",
            [
                "kb_version",
                "build_seq",
                "status",
                "started_at",
                "duration_seconds",
                "sources_seen",
                "sources_changed",
                "artifacts_created",
                "llm_calls",
                "embedding_calls",
                "llm_calls_per_changed_source",
                "extractor_failures",
                "failed_gate",
                "gate_measured_value",
                "is_active",
            ],
            data.build_health,
            "no build runs recorded",
        ),
        _section(
            "Budget breaches (per run x agent)",
            [
                "run_id",
                "agent_name",
                "events",
                "tokens_charged",
                "run_tokens",
                "run_budget_tokens",
                "over_run_budget",
                "follow_up_requests",
                "follow_up_tokens",
                "agent_max_requests",
                "agent_max_tokens",
                "over_agent_requests",
                "over_agent_tokens",
            ],
            data.budget_breaches,
            "no budget breaches — all runs and agents within .claude/rules/token-budgets.md limits",
        ),
    ]


# --- Markdown --------------------------------------------------------------------


def render_markdown(data: DashboardData) -> str:
    lines = [
        "# KB operator dashboard (ADR-0014 Phase 1)",
        "",
        f"Generated: {data.generated_at.isoformat(timespec='seconds')}  ",
        f"Database: `{data.database}` (read-only, aggregate-only)",
        "",
        "## At a glance",
        "",
    ]
    for tile in _build_tiles(data):
        marker = {"ok": "OK", "warn": "WARN", "bad": "FAIL"}[tile.status]
        lines.append(f"- [{marker}] {tile.label}: **{tile.value}**")
    for section in _sections(data):
        lines += ["", f"## {section.title}", ""]
        if not section.rows:
            lines.append(f"_{section.empty_note}_")
            continue
        lines.append("| " + " | ".join(section.headers) + " |")
        lines.append("|" + "---|" * len(section.headers))
        lines += ["| " + " | ".join(row) + " |" for row in section.rows]
    lines.append("")
    return "\n".join(lines)


# --- HTML ------------------------------------------------------------------------

_CSS = """
body { font-family: -apple-system, "Segoe UI", sans-serif; margin: 2rem; color: #1a1a1a; }
h1 { font-size: 1.4rem; } h2 { font-size: 1.1rem; margin-top: 2rem; }
.meta { color: #555; font-size: 0.85rem; }
.tiles { display: flex; flex-wrap: wrap; gap: 0.6rem; margin: 1rem 0; }
.tile { border-radius: 6px; padding: 0.6rem 0.9rem; min-width: 12rem; }
.tile .label { font-size: 0.75rem; color: #444; }
.tile .value { font-size: 1.05rem; font-weight: 600; }
.ok { background: #e6f4ea; border: 1px solid #34a853; }
.warn { background: #fef7e0; border: 1px solid #f9ab00; }
.bad { background: #fce8e6; border: 1px solid #d93025; }
table { border-collapse: collapse; font-size: 0.8rem; }
th, td { border: 1px solid #ddd; padding: 0.3rem 0.5rem; text-align: right; }
th { background: #f1f3f4; } td:first-child, th:first-child { text-align: left; }
.empty { color: #777; font-style: italic; }
"""


def _html_tiles(tiles: list[Tile]) -> str:
    parts = ['<div class="tiles">']
    for tile in tiles:
        parts.append(
            f'<div class="tile {tile.status}"><div class="label">{escape(tile.label)}</div>'
            f'<div class="value">{escape(tile.value)}</div></div>'
        )
    parts.append("</div>")
    return "".join(parts)


def _html_section(section: Section) -> str:
    parts = [f"<h2>{escape(section.title)}</h2>"]
    if not section.rows:
        parts.append(f'<p class="empty">{escape(section.empty_note)}</p>')
        return "".join(parts)
    header = "".join(f"<th>{escape(h)}</th>" for h in section.headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{escape(cell)}</td>" for cell in row) + "</tr>"
        for row in section.rows
    )
    parts.append(f"<table><thead><tr>{header}</tr></thead><tbody>{body}</tbody></table>")
    return "".join(parts)


def render_html(data: DashboardData) -> str:
    sections = "".join(_html_section(section) for section in _sections(data))
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<title>KB operator dashboard</title>"
        f"<style>{_CSS}</style></head><body>"
        "<h1>KB operator dashboard (ADR-0014 Phase 1)</h1>"
        f'<p class="meta">Generated {escape(data.generated_at.isoformat(timespec="seconds"))} '
        f"&middot; database {escape(data.database)} &middot; read-only, aggregate-only</p>"
        f"{_html_tiles(_build_tiles(data))}"
        f"{sections}"
        "</body></html>"
    )


# --- entry point -----------------------------------------------------------------


async def generate_dashboard(
    database_url: str, out_dir: Path, *, report_path: Path | None = None
) -> tuple[Path, Path]:
    """Fetch the views, render, and write dashboard.html + dashboard.md under out_dir."""
    data = await fetch_dashboard(database_url, report_path=report_path)
    out_dir.mkdir(parents=True, exist_ok=True)
    html_path = out_dir / "dashboard.html"
    md_path = out_dir / "dashboard.md"
    html_path.write_text(render_html(data), encoding="utf-8")
    md_path.write_text(render_markdown(data), encoding="utf-8")
    logger.info(
        "dashboard.generated html=%s md=%s database=%s days=%d builds=%d breaches=%d golden=%s",
        html_path,
        md_path,
        data.database,
        len(data.retrieval_health),
        len(data.build_health),
        len(data.budget_breaches),
        data.golden is not None,
    )
    return html_path, md_path
