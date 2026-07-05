"""eval_task_context — live two-arm A/B for get_task_context (PR-39, ADR-0030 §2).

For each case in `evals/agent_task_cases/task_context_ab_v1.yaml` (10 realistic dev
tasks with hand-written expected files), run the SAME model + task through two arms,
kb_agent.py-style (an LLM, a loop, and a few tools):

    tooled  — read tools + ONE get_task_context call (the real broker path, in-process:
              LangGraph fan-out, alias index, blast radius, ledger row and all)
    raw     — read tools only (list_files / read_file / read_full), no KB

and score each arm's expected-file coverage: a file counts when the agent read it or
named it in its final answer (for the tooled arm the tool-surfaced paths are also
reported separately, as `tool_cover`). Report per case + aggregate: coverage, steps,
file reads, tokens.

LIVE EXECUTION NEEDS LLM CREDS + A BUILT LOCAL KB — the hermetic, credential-free gate
for the same golden set is `evals/tests/test_task_context_ab.py` (fixture-seeded,
asserts tool-output coverage). To run live:

    cd services/kb-builder && DATABASE_URL=postgresql+asyncpg://$USER@localhost:5432/agentic_kb \
        uv run python -m agentic_kb_builder.build   # if the KB is not built yet
    cd services/mcp-server
    export DATABASE_URL=postgresql+asyncpg://$USER@localhost:5432/agentic_kb
    export LLM_PROVIDER=groq GROQ_API_KEY=...       # or openai / anthropic, see kb_agent.py
    uv run python ../../scripts/eval_task_context.py            # both arms, all cases
    uv run python ../../scripts/eval_task_context.py --arm raw --limit 3
    uv run python ../../scripts/eval_task_context.py --case kb-search-dual-budget
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from pathlib import Path
from typing import Any

import kb_agent  # sibling module in scripts/ (loads .env, owns the LLM client + loop glue)

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "evals"))  # harness.task_context_ab (case loader)

from harness.task_context_ab import TaskContextAbCase, load_ab_cases  # noqa: E402

CASES_PATH = _REPO_ROOT / "evals" / "agent_task_cases" / "task_context_ab_v1.yaml"
MAX_STEPS = kb_agent.MAX_STEPS

TOOLED_PROMPT = (
    "You are a coding agent working in a repository. Call the `get_task_context` tool FIRST "
    "with the full task description (and any file/symbol names the task already mentions as "
    "hints) — ONE call gives you the resolved files in scope, their blast radius (callers, "
    "callees, tests), conventions, and similar prior changes, each with a confidence tier. "
    "Trust `deterministic` items; verify `interpreted` items (read the file) before relying "
    "on them; treat ambiguous_candidates/open_questions as questions to resolve, never guesses. "
    "Only read files the tool did not already cover. Finish with a short plan and a line "
    "starting with 'FILES:' listing every repo-relative file you would change or consult."
)

RAW_PROMPT = (
    "You are a coding agent working in a repository. Find the context you need by exploring "
    "with `list_files`, `read_file`, and `read_full`. Finish with a short plan and a line "
    "starting with 'FILES:' listing every repo-relative file you would change or consult."
)

_READ_TOOLS = [
    t for t in kb_agent._FILE_TOOLS if t["name"] in ("read_file", "read_full", "list_files")
]

_TASK_CONTEXT_TOOL = {
    "name": "get_task_context",
    "description": (
        "For a coding task, get everything at once in ONE call: resolved files/symbols in "
        "scope, blast radius (callers/callees/tests), conventions, similar prior changes — "
        "tiered by confidence and cited. Call this FIRST instead of exploring files."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "task_description": {"type": "string"},
            "file_paths": {"type": "array", "items": {"type": "string"}},
            "symbols": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["task_description"],
    },
}

_PATHISH = re.compile(r"[A-Za-z0-9_\-./]+/[A-Za-z0-9_\-./]+\.[A-Za-z0-9]+")


def _normalize(path: str) -> str:
    """Repo-relative path for coverage scoring: a locally-built KB stores absolute
    local paths (local-repo connector), while expected_files are repo-relative."""
    prefix = str(_REPO_ROOT) + "/"
    return path[len(prefix):] if path.startswith(prefix) else path.lstrip("./")


def _render_tool_response(response: Any) -> tuple[str, set[str]]:
    """Compact, citable text for the model + the set of paths the tool surfaced."""
    lines: list[str] = []
    surfaced: set[str] = set()
    lines.append("RESOLVED SCOPE:")
    for entity in response.resolved_scope.entities:
        path = _normalize(entity.path)
        surfaced.add(path)
        symbol = f" :: {entity.symbol}" if entity.symbol else ""
        lines.append(f"- {path}{symbol} [{entity.confidence_tier}, {entity.resolution_source}]")
    for candidate in response.resolved_scope.ambiguous_candidates:
        lines.append(f"- AMBIGUOUS {candidate.alias_text!r}: {candidate.reason}")
    for bucket in ("callers", "callees", "tests"):
        entries = getattr(response.blast_radius, bucket)
        if entries:
            lines.append(f"BLAST RADIUS ({bucket}):")
            for entry in entries:
                path = _normalize(entry.path)
                surfaced.add(path)
                caveat = f" CAVEAT: {entry.caveat}" if entry.caveat else ""
                lines.append(f"- {path} [{entry.edge_type}, {entry.confidence_tier}]{caveat}")
    if response.conventions:
        lines.append("CONVENTIONS:")
        lines.extend(f"- {convention.pattern}" for convention in response.conventions)
    if response.similar_prior_changes:
        lines.append("SIMILAR PRIOR CHANGES:")
        lines.extend(
            f"- {change.commit_or_pr_id}: {change.summary}"
            for change in response.similar_prior_changes
        )
    if response.open_questions:
        lines.append("OPEN QUESTIONS:")
        lines.extend(f"- {question}" for question in response.open_questions)
    lines.append(
        f"(budget_used: {response.budget_used.tokens} tokens, "
        f"{response.budget_used.calls} retrieval calls)"
    )
    return "\n".join(lines), surfaced


async def _call_task_context(args: dict[str, Any]) -> tuple[str, set[str]]:
    """The real broker tool, in-process (PostgresKeywordSearchClient over DATABASE_URL)."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from agentic_mcp_server.auth.rbac import Requester
    from agentic_mcp_server.context_broker.dependencies import BrokerDeps
    from agentic_mcp_server.context_broker.task_context import get_task_context
    from agentic_mcp_server.infrastructure.postgres.keyword_search import (
        PostgresKeywordSearchClient,
    )
    from agentic_mcp_server.mcp.tool_schemas.task_context import (
        GetTaskContextRequest,
        TaskContextHints,
    )

    engine = create_async_engine(os.environ["DATABASE_URL"])
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        deps = BrokerDeps(
            session_factory=factory, search_client=PostgresKeywordSearchClient(factory)
        )
        hints = None
        if args.get("file_paths") or args.get("symbols"):
            hints = TaskContextHints(
                file_paths=list(args.get("file_paths") or []),
                symbols=list(args.get("symbols") or []),
            )
        response = await get_task_context(
            deps,
            GetTaskContextRequest(task_description=args["task_description"], hints=hints),
            Requester(subject="eval-task-context", teams=frozenset()),
        )
        return _render_tool_response(response)
    finally:
        await engine.dispose()


async def _run_arm(case: TaskContextAbCase, arm: str) -> dict[str, Any]:
    client, provider, model = kb_agent._make_client()
    tooled = arm == "tooled"
    system = TOOLED_PROMPT if tooled else RAW_PROMPT
    tools = ([_TASK_CONTEXT_TOOL] if tooled else []) + list(_READ_TOOLS)

    if kb_agent._is_openai(provider):
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": case.task},
        ]
    else:
        messages = [{"role": "user", "content": case.task}]

    seen_paths: set[str] = set()  # files read + tool-surfaced (tooled)
    tool_surfaced: set[str] = set()
    answer_text = ""
    in_tok = out_tok = steps = tool_calls = file_reads = retries_recovered = 0
    model_error = ""

    for _step in range(MAX_STEPS):
        # Same guard as kb_agent's own loop: small models sometimes hallucinate a tool
        # name and the provider 400s. kb_agent._model_step already retries that ONCE,
        # feeding the verbatim provider error back before giving up (evaluation-system.md
        # §2). A recovered retry costs this arm nothing but the extra call (counted
        # below); only an EXHAUSTED retry (both attempts failed) still ends the arm
        # early and is flagged so the report can call it out — one flaky generation
        # must cost ONE arm-run its remaining steps, never the whole eval.
        try:
            native, answer, tool_uses, di, do, retried = kb_agent._model_step(
                client, provider, model, system, tools, messages
            )
        except Exception as exc:
            model_error = f"{type(exc).__name__}: {exc}"
            print(f"  · [model error, arm ends early: {model_error[:120]}]")
            break
        if retried:
            retries_recovered += 1
            print("  · [recovered from a provider 400 after one bounded retry]")
        steps += 1
        in_tok += di
        out_tok += do
        if answer.strip():
            answer_text += "\n" + answer
        if not tool_uses:
            break
        messages.append(native)
        pairs: list[tuple[dict[str, Any], str]] = []
        for tu in tool_uses:
            name, args = tu["name"], tu["args"]
            try:
                if name == "get_task_context":
                    tool_calls += 1
                    out, surfaced = await _call_task_context(args)
                    tool_surfaced |= surfaced
                    seen_paths |= surfaced
                    print(f"  · get_task_context({str(args.get('task_description', ''))[:60]!r})")
                elif name in ("read_file", "read_full", "list_files"):
                    file_reads += 1
                    if name != "list_files" and "path" in args:
                        seen_paths.add(_normalize(str(args["path"])))
                    fn = {
                        "read_file": kb_agent.read_file,
                        "read_full": kb_agent.read_full,
                        "list_files": kb_agent.list_files,
                    }[name]
                    out = str(fn(**args))
                    print(f"  · {name}({', '.join(f'{k}={v!r}'[:60] for k, v in args.items())})")
                else:
                    out = f"error: unknown tool {name}"
            except Exception as exc:  # a bad tool call must inform the model, not crash the run
                out = f"error: {type(exc).__name__}: {exc}"
                print(f"  · {name} -> {out}")
            pairs.append((tu, out))
        messages.extend(kb_agent._tool_result_messages(provider, pairs))
    mentioned = set(_PATHISH.findall(answer_text))
    covered = {f for f in case.expected_files if f in seen_paths or f in mentioned}
    return {
        "arm": arm,
        "model_error": model_error,
        "retries_recovered": retries_recovered,
        "coverage": len(covered) / len(case.expected_files),
        "tool_cover": (
            len([f for f in case.expected_files if f in tool_surfaced])
            / len(case.expected_files)
        ),
        "steps": steps,
        "tool_calls": tool_calls,
        "file_reads": file_reads,
        "tokens": in_tok + out_tok,
    }


async def main() -> int:
    parser = argparse.ArgumentParser(description="Two-arm A/B for get_task_context (PR-39).")
    parser.add_argument("--arm", choices=["tooled", "raw", "both"], default="both")
    parser.add_argument("--case", help="run a single case id")
    parser.add_argument("--limit", type=int, help="run only the first N cases")
    args = parser.parse_args()

    if "DATABASE_URL" not in os.environ:
        print("DATABASE_URL is required (a built local KB). See the module docstring.")
        return 1

    cases = load_ab_cases(CASES_PATH)
    if args.case:
        cases = [case for case in cases if case.id == args.case]
        if not cases:
            print(f"unknown case id {args.case!r}")
            return 1
    if args.limit:
        cases = cases[: args.limit]
    arms = ["tooled", "raw"] if args.arm == "both" else [args.arm]

    rows: list[dict[str, Any]] = []
    for case in cases:
        print(f"\n## {case.id}  (expected files: {len(case.expected_files)})")
        for arm in arms:
            print(f"-- arm={arm}")
            result = await _run_arm(case, arm)
            result["case"] = case.id
            rows.append(result)
            print(
                f"   coverage={result['coverage']:.2f} tool_cover={result['tool_cover']:.2f} "
                f"steps={result['steps']} reads={result['file_reads']} tokens={result['tokens']} "
                f"retries_recovered={result['retries_recovered']}"
            )

    print("\n=== AGGREGATE (mean over cases) ===")
    for arm in arms:
        arm_rows = [row for row in rows if row["arm"] == arm]
        if not arm_rows:
            continue
        def mean(key: str, rows_for_arm: list[dict[str, Any]] = arm_rows) -> float:
            return sum(row[key] for row in rows_for_arm) / len(rows_for_arm)

        def total(key: str, rows_for_arm: list[dict[str, Any]] = arm_rows) -> int:
            return sum(row[key] for row in rows_for_arm)

        flakes = sum(1 for row in arm_rows if row["model_error"])
        print(
            f"   {arm:7s} coverage={mean('coverage'):.3f} tool_cover={mean('tool_cover'):.3f} "
            f"steps={mean('steps'):.1f} reads={mean('file_reads'):.1f} tokens={mean('tokens'):.0f} "
            f"retries_recovered={total('retries_recovered')} flakes={flakes}/{len(arm_rows)}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
