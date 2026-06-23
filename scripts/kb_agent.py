"""kb_agent — the simple version (ADR-0025).

A minimal KB-first coding agent: an LLM, a loop, and five tools. No MCP server, no
Context Broker, no evidence-pack ceremony. The model is told to try the knowledge base
FIRST and only read files when the KB isn't enough — but it keeps its native tools, so it
is never crippled. The single restriction is a budget: it gets a fixed number of
``kb_search`` calls; when they run out the tool is withdrawn and it reads files directly.

This is deliberately ~300 lines (cf. Thorsten Ball's "an LLM, a loop, and enough tokens").

Run a real task (needs LLM creds + the active KB in Postgres):
    cd services/mcp-server && uv run python ../../scripts/kb_agent.py "how is build_seq resolved?"

Verify the KB wiring WITHOUT an LLM key (search only), from the same dir:
    uv run python ../../scripts/kb_agent.py --search graphify

Measure the KB's benefit (same model + task, KB on vs off):
    uv run python ../../scripts/kb_agent.py "how is build_seq resolved?"            # KB-first
    uv run python ../../scripts/kb_agent.py --no-kb "how is build_seq resolved?"    # baseline
Each run prints a report (tokens in/out/total, steps, kb_search calls, file reads).

Env:
    DATABASE_URL      postgresql+asyncpg://user:pass@host:port/agentic_kb  (the active KB)
    LLM_PROVIDER      groq | openai | anthropic_foundry | anthropic   (default groq)
    GROQ_API_KEY      Groq key (or LLM_API_KEY); used when provider=groq
    LLM_MODEL         model id (default groq: llama-3.3-70b-versatile; anthropic: required)
    LLM_BASE_URL      override; anthropic_foundry needs the .../anthropic endpoint
    LLM_CA_CERT / SSL_CERT_FILE   corporate CA bundle (Zscaler), optional
    KB_SEARCH_BUDGET  max kb_search calls per task (default 4)
    KB_AGENT_MAX_STEPS  hard step cap (default 20)
    REPO_ROOT         repo the file tools operate on (default: this repo)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import codeskeleton  # sibling module in scripts/ (on sys.path[0] when run as a script)
import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


def _load_dotenv() -> None:
    """Load repo-root .env into os.environ (shell env wins; quotes + inline comments stripped)."""
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        if not key or key in os.environ:
            continue
        val = val.strip()
        if val[:1] in ("'", '"') and val[-1:] == val[:1]:
            val = val[1:-1]
        elif " #" in val:
            val = val.split(" #", 1)[0].strip()
        os.environ[key] = val


_load_dotenv()

REPO_ROOT = Path(os.environ.get("REPO_ROOT", Path(__file__).resolve().parent.parent)).resolve()
SEARCH_BUDGET = int(os.environ.get("KB_SEARCH_BUDGET", "4"))
# ADR-0025 section 4: the one enforced restriction is "call count + token budget". The token cap
# bounds total KB bytes pulled even if each call is cheap (token-budgets.md: 3k-4k for impl agent).
KB_TOKEN_BUDGET = int(os.environ.get("KB_SEARCH_TOKEN_BUDGET", "3000"))
MAX_STEPS = int(os.environ.get("KB_AGENT_MAX_STEPS", "20"))
_TOP = 6

SYSTEM_PROMPT = (
    "You are a coding agent working in a repository.\n\n"
    "KNOWLEDGE BASE FIRST, FILES SECOND. Prefer the `kb_search` tool to find the RIGHT "
    "code and docs fast across the whole repo. If the search results already answer the "
    "question or tell you exactly which files matter, use them and cite the sources — do "
    "NOT re-read what search already gave you. If the KB is missing, partial, or stale, or "
    "you need exact current code to make a change, THEN use `read_file` on the specific "
    "files. The KB points you at the right place; the file gives exact truth.\n\n"
    "You have a limited number of kb_search calls — make each one count, then proceed. "
    "When you cite, use the source path. Finish with a short, direct answer (and a Sources "
    "line). Do not invent files, symbols, or APIs you have not seen.\n\n"
    "WHENEVER you create or edit code, run the `lint` tool on that file and fix anything it "
    "reports (edit_file again) BEFORE you finish — this is the repo's pre-commit standard. Do not "
    "end the task with lint issues outstanding."
)

# The baseline (--no-kb): a plain agent with native tools only and no knowledge base. Used to
# measure the KB's effect by holding the model + task constant and removing only kb_search.
BASELINE_PROMPT = (
    "You are a coding agent working in a repository. Find the context you need by exploring the "
    "files with `list_files` and `read_file`, then answer. When you cite, use the file path. "
    "Finish with a short, direct answer (and a Sources line). Do not invent files, symbols, or "
    "APIs you have not seen."
)


# --------------------------------------------------------------------------- KB search


def _tokens(query: str) -> list[str]:
    raw = "".join(c if c.isalnum() or c.isspace() else " " for c in query.lower())
    return list(dict.fromkeys(t for t in raw.split() if len(t) > 1))[:12]


def _search_sql(n: int) -> str:
    score = " + ".join(
        f"(CASE WHEN a.title ILIKE :t{i} THEN 4.0 ELSE 0.0 END"
        f" + CASE WHEN a.search_text ILIKE :t{i} THEN 1.5 ELSE 0.0 END"
        f" + CASE WHEN a.body_text ILIKE :t{i} THEN 1.0 ELSE 0.0 END)"
        for i in range(n)
    )
    match = " OR ".join(
        f"a.title ILIKE :t{i} OR a.search_text ILIKE :t{i} OR a.body_text ILIKE :t{i}"
        for i in range(n)
    )
    return (
        f"SELECT a.title, a.artifact_type, a.body_text, s.source_uri, ({score}) AS score "
        "FROM knowledge_artifact a JOIN source_item s ON s.source_id = a.source_id "
        "WHERE a.valid_from_seq <= :build_seq "
        "AND (a.invalidated_at_seq IS NULL OR a.invalidated_at_seq > :build_seq) "
        "AND s.is_deleted = false "
        f"AND ({match}) "
        "ORDER BY score DESC, a.artifact_id LIMIT :top"
    )


async def kb_search(sessions: async_sessionmaker, build_seq: int, query: str) -> str:
    """Ranked keyword search over the active KB. Returns a compact, citable result list."""
    tokens = _tokens(query)
    if not tokens:
        return "No searchable terms in the query."
    params: dict[str, Any] = {f"t{i}": f"%{tok}%" for i, tok in enumerate(tokens)}
    params.update(build_seq=build_seq, top=_TOP)
    async with sessions() as session:
        rows = (await session.execute(text(_search_sql(len(tokens))), params)).all()
    if not rows:
        return f"KB search for {query!r}: no results. Read files directly to answer."
    lines = [f"KB search for {query!r} — top {len(rows)} (cite source_uri):"]
    for r in rows:
        snippet = " ".join((r.body_text or "").split())[:200]
        title = r.title or r.artifact_type
        lines.append(f"- [{r.source_uri}] {title} ({r.artifact_type}): {snippet}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- file tools


def _safe(path: str) -> Path:
    target = (REPO_ROOT / path).resolve()
    # is_relative_to (not string startswith): a string prefix check lets a sibling dir whose
    # name starts with REPO_ROOT (e.g. "<root>-secrets/") escape the sandbox.
    if target != REPO_ROOT and not target.is_relative_to(REPO_ROOT):
        raise ValueError(f"path escapes REPO_ROOT: {path}")
    return target


def _truncate(body: str, cap: int) -> str:
    """Cap a file read, marking the cut so a truncated read is never mistaken for the whole file
    (ADR-0026: read_full is the exact/citation path — silent truncation could misquote)."""
    if len(body) <= cap:
        return body
    return body[:cap] + f"\n# ...truncated at {cap} chars; file continues — read in ranges."


def _kb_budget_open(searches_left: int, kb_tokens_left: int) -> bool:
    """The kb_search tool is offered AND honored only while BOTH caps remain — the single
    source of truth for ADR-0025 §4's one enforced restriction (call count + token budget)."""
    return searches_left > 0 and kb_tokens_left > 0


# Compression state (ADR-0026). COMPRESS is toggled off by --no-compress for the A/B baseline.
# STATS accumulates how many tokens skeletonization saved across a run, for the report.
COMPRESS = True
STATS = {"compress_saved_tokens": 0, "skeletons": 0}
_CODE_SUFFIXES = {".py", ".pyi", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".cpp", ".c"}
_READ_CHAR_CAP = 8000  # skeleton/raw read cap; read_full uses 2x for the exact body being edited


def read_file(path: str) -> str:
    """Read a file. By default returns the COMPRESSED skeleton of code (signatures + structure,
    bodies elided) so the model can orient cheaply; it calls read_full for the exact body it needs.
    With --no-compress (or for non-code files) it returns the raw text."""
    try:
        raw = _safe(path).read_text(encoding="utf-8")
    except OSError as exc:
        return f"error: {exc}"
    if COMPRESS and Path(path).suffix.lower() in _CODE_SUFFIXES:
        result = codeskeleton.skeletonize(raw, filename=Path(path).name)
        if result.saved_tokens > 0:
            STATS["compress_saved_tokens"] += result.saved_tokens
            STATS["skeletons"] += 1
            return (
                f"# SKELETON of {path} ({result.saved_pct:.0f}% smaller; "
                f"call read_full for the exact body of anything you edit)\n{result.text}"
            )
    return _truncate(raw, _READ_CHAR_CAP)


def read_full(path: str) -> str:
    """Return the EXACT, full text of a file (the reversible original) — for the body the model
    will actually edit or must quote precisely. No compression."""
    try:
        return _truncate(_safe(path).read_text(encoding="utf-8"), 2 * _READ_CHAR_CAP)
    except OSError as exc:
        return f"error: {exc}"


def list_files(directory: str = ".") -> str:
    try:
        base = _safe(directory)
        names = sorted(p.name + ("/" if p.is_dir() else "") for p in base.iterdir())
        return "\n".join(names) or "(empty)"
    except OSError as exc:
        return f"error: {exc}"


def edit_file(path: str, old_str: str, new_str: str) -> str:
    """Replace old_str with new_str (old_str must match exactly once). An empty old_str ONLY
    CREATES a brand-new file — it will NOT overwrite an existing one. To ADD code to an existing
    file, pass a unique nearby line as old_str and include it in new_str."""
    target = _safe(path)
    try:
        body = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        body = ""
    except OSError as exc:
        return f"error: {exc}"
    if not old_str:
        # Empty old_str is create-only. Refusing to overwrite an existing file is the guardrail:
        # the model used to wipe whole modules by passing old_str="" to "add" a function.
        if body:
            return (
                "error: file already exists and is non-empty — empty old_str will NOT overwrite "
                "it. To add code, pass a unique existing line as old_str and repeat it in new_str."
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(new_str, encoding="utf-8")
        return f"created {path}"
    if body.count(old_str) != 1:
        return f"error: old_str must match exactly once (found {body.count(old_str)})."
    target.write_text(body.replace(old_str, new_str), encoding="utf-8")
    return f"edited {path}"


def run_tests(path: str) -> str:
    proc = subprocess.run(
        ["uv", "run", "pytest", path, "-q"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    return (proc.stdout + "\n" + proc.stderr).strip()[-2000:]


def lint(path: str) -> str:
    """Run ruff (the repo's pre-commit linter) with auto-fix on a file, then report what is left.

    Auto-fixable issues (e.g. unused imports) are fixed in place; anything ruff cannot fix
    automatically (e.g. a line too long) is reported so the agent must edit_file to fix it before
    finishing. `ruff` is on PATH inside the project's uv venv that launches this agent."""
    target = _safe(path)
    proc = subprocess.run(
        ["ruff", "check", "--fix", str(target)],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    out = (proc.stdout + proc.stderr).strip()
    return out[-2000:] if out else "ruff: clean (no issues remaining)"


# --------------------------------------------------------------------------- tool schemas

_FILE_TOOLS = [
    {
        "name": "read_file",
        "description": (
            "Read a file (repo-relative path). For code you get a SKELETON — signatures, types, "
            "and structure with bodies elided — which is enough to orient and write code that "
            "fits. Call read_full only for the exact body of something you will edit or quote."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "read_full",
        "description": (
            "Read the EXACT, full text of a file (no skeleton). Use for the 1-2 files/bodies you "
            "actually edit or must quote precisely."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "list_files",
        "description": "List entries in a repo-relative directory.",
        "input_schema": {
            "type": "object",
            "properties": {"directory": {"type": "string"}},
            "required": [],
        },
    },
    {
        "name": "edit_file",
        "description": (
            "Replace old_str with new_str in a file (old_str must match exactly once). To ADD code "
            "to an EXISTING file, set old_str to a unique nearby line and repeat that line in "
            "new_str. Empty old_str ONLY creates a brand-new file; it will NOT overwrite an "
            "existing one (that would wipe the file)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_str": {"type": "string"},
                "new_str": {"type": "string"},
            },
            "required": ["path", "old_str", "new_str"],
        },
    },
    {
        "name": "run_tests",
        "description": "Run `pytest <path> -q` and return the output tail.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "lint",
        "description": (
            "Run the repo linter (ruff, auto-fix) on a file you created or edited. Auto-fixable "
            "issues are fixed; anything left is reported and you MUST edit_file to fix it. Always "
            "lint code you wrote before finishing."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
]

_KB_TOOL = {
    "name": "kb_search",
    "description": (
        "Search the knowledge base (code + docs + tickets) for the right files/answers. "
        "Prefer this FIRST. Budgeted — a limited number of calls per task."
    ),
    "input_schema": {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    },
}


# --------------------------------------------------------------------------- LLM client


def _http_client() -> httpx.Client | None:
    ca = os.environ.get("LLM_CA_CERT") or os.environ.get("SSL_CERT_FILE")
    return httpx.Client(verify=ca) if ca else None


def _is_openai(provider: str) -> bool:
    """Groq / OpenAI / any OpenAI-compatible chat-completions + tool-calling endpoint."""
    return provider in ("groq", "openai", "openai_compatible")


def _make_client() -> tuple[Any, str, str]:
    """Return (client, provider, model). Default provider is groq (cheap, fast, instrumented)."""
    provider = os.environ.get("LLM_PROVIDER", "groq")
    if _is_openai(provider):
        from openai import OpenAI

        base = os.environ.get(
            "LLM_BASE_URL",
            "https://api.groq.com/openai/v1" if provider == "groq" else "https://api.openai.com/v1",
        )
        key = os.environ.get("LLM_API_KEY") or os.environ.get("GROQ_API_KEY", "")
        model = os.environ.get("LLM_MODEL", "llama-3.3-70b-versatile")
        return OpenAI(base_url=base, api_key=key, http_client=_http_client()), provider, model
    model = os.environ["LLM_MODEL"]
    if provider == "anthropic_foundry":
        from anthropic import AnthropicFoundry

        client = AnthropicFoundry(
            base_url=os.environ["LLM_BASE_URL"],
            api_key=os.environ["LLM_API_KEY"],
            http_client=_http_client(),
        )
    else:
        from anthropic import Anthropic

        client = Anthropic(api_key=os.environ["LLM_API_KEY"], http_client=_http_client())
    return client, provider, model


def _openai_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {"type": "function", "function": {
            "name": t["name"], "description": t["description"], "parameters": t["input_schema"]}}
        for t in tools
    ]


def _model_step(
    client: Any, provider: str, model: str, system: str, tools: list[dict[str, Any]],
    messages: list[dict[str, Any]],
) -> tuple[dict[str, Any], str, list[dict[str, Any]], int, int]:
    """One model call. Returns (assistant_message_native, text, tool_uses, in_tokens, out_tokens).

    tool_uses are provider-agnostic dicts: {"id", "name", "args"}. The caller appends the returned
    assistant_message_native to its provider-native message list, runs the tools, then appends the
    tool-result messages from _tool_result_messages."""
    if _is_openai(provider):
        resp = client.chat.completions.create(
            model=model, max_tokens=4096, messages=messages,
            tools=_openai_tools(tools) or None, tool_choice="auto",
        )
        msg = resp.choices[0].message
        tool_uses = [
            {"id": tc.id, "name": tc.function.name,
             "args": json.loads(tc.function.arguments or "{}")}
            for tc in (msg.tool_calls or [])
        ]
        u = resp.usage
        native = msg.model_dump(exclude_none=True)
        return native, msg.content or "", tool_uses, u.prompt_tokens, u.completion_tokens
    resp = client.messages.create(
        model=model, max_tokens=4096, system=system, tools=tools, messages=messages,
    )
    text = "".join(b.text for b in resp.content if b.type == "text")
    tool_uses = [
        {"id": b.id, "name": b.name, "args": dict(b.input)}
        for b in resp.content if b.type == "tool_use"
    ]
    native = {"role": "assistant", "content": [b.model_dump() for b in resp.content]}
    return native, text, tool_uses, resp.usage.input_tokens, resp.usage.output_tokens


def _tool_result_messages(
    provider: str, pairs: list[tuple[dict[str, Any], str]]
) -> list[dict[str, Any]]:
    """Native messages carrying tool outputs. OpenAI: one `tool` message each; Anthropic: one
    `user` message with all tool_result blocks."""
    if _is_openai(provider):
        return [{"role": "tool", "tool_call_id": tu["id"], "content": out} for tu, out in pairs]
    return [{"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": tu["id"], "content": out} for tu, out in pairs]}]


# --------------------------------------------------------------------------- the loop


async def run_task(task: str, *, use_kb: bool = True) -> int:
    STATS["compress_saved_tokens"] = 0  # reset per run (module-level for a single-task script)
    STATS["skeletons"] = 0
    engine = create_async_engine(os.environ["DATABASE_URL"])
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        active = (
            await session.execute(
                text("SELECT kb_version, build_seq FROM kb_build_run WHERE status = 'active'")
            )
        ).one_or_none()
    if active is None:
        print("No active KB version. Build + activate one first.")
        return 1

    client, provider, model = _make_client()
    system = SYSTEM_PROMPT if use_kb else BASELINE_PROMPT
    mode = "KB-first" if use_kb else "baseline (no KB)"
    print(f"mode={mode} | provider={provider} model={model} | KB {active.kb_version} "
          f"(seq={active.build_seq}) | search budget {SEARCH_BUDGET if use_kb else 0}\n")

    # provider-native message list: OpenAI carries the system prompt as a message; Anthropic
    # passes it as a separate param (so its list starts with just the user turn).
    if _is_openai(provider):
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system}, {"role": "user", "content": task}]
    else:
        messages = [{"role": "user", "content": task}]

    searches_left = SEARCH_BUDGET if use_kb else 0
    kb_tokens_left = KB_TOKEN_BUDGET if use_kb else 0
    in_tok = out_tok = steps = kb_calls = file_reads = 0

    for _step in range(MAX_STEPS):
        # offer kb_search only while BOTH caps (call count AND token budget) remain (ADR-0025 §4)
        kb_open = _kb_budget_open(searches_left, kb_tokens_left)
        tools = list(_FILE_TOOLS) + ([_KB_TOOL] if kb_open else [])
        try:
            native, answer, tool_uses, di, do = _model_step(
                client, provider, model, system, tools, messages)
        except Exception as exc:  # small models sometimes emit invalid tool calls (provider 400)
            print(f"\n[model error: {type(exc).__name__}: {exc}]")
            break
        steps += 1
        in_tok += di
        out_tok += do
        if answer.strip():
            # TODO(ADR-0026): this answer is NOT citation-verified. Code reads may be skeletons
            # (lossy); before treating any answer as citation-grade, wire context.verify_answer so
            # quoted claims are checked against the exact original, not a skeleton.
            print(answer)
        if not tool_uses:
            break

        messages.append(native)
        pairs: list[tuple[dict[str, Any], str]] = []
        for tu in tool_uses:
            name, args = tu["name"], tu["args"]
            try:
                if name == "kb_search":
                    # gate INSIDE the loop: one assistant turn can emit several kb_search calls,
                    # so re-check both caps per call or a parallel burst overruns the budget.
                    if not _kb_budget_open(searches_left, kb_tokens_left):
                        out = ("KB budget spent — work with what you have, or read the specific "
                               "files you still need.")
                        print("  · kb_search blocked (budget spent)")
                    else:
                        searches_left -= 1
                        kb_calls += 1
                        out = await kb_search(sessions, active.build_seq, args.get("query", ""))
                        kb_tokens_left -= codeskeleton.estimate_tokens(out)
                        if searches_left <= 0 or kb_tokens_left <= 0:
                            out += "\n\n(KB budget spent — read the specific files you still need.)"
                        left = f"{max(searches_left, 0)} calls, ~{max(kb_tokens_left, 0)} tok"
                        print(f"  · kb_search({args.get('query', '')!r})  [{left} left]")
                else:
                    if name in ("read_file", "read_full", "list_files"):
                        file_reads += 1
                    fn = {"read_file": read_file, "read_full": read_full,
                          "list_files": list_files, "edit_file": edit_file,
                          "run_tests": run_tests, "lint": lint}[name]
                    out = str(fn(**args))
                    print(f"  · {name}({', '.join(f'{k}={v!r}'[:60] for k, v in args.items())})")
            except Exception as exc:  # a bad tool call must inform the model, not crash the run
                out = f"error: {type(exc).__name__}: {exc}"
                print(f"  · {name} -> {out}")
            pairs.append((tu, out))
        messages.extend(_tool_result_messages(provider, pairs))
    else:
        print("\n[step cap reached]")

    compress_note = (
        f"compression: {STATS['skeletons']} skeleton(s), ~{STATS['compress_saved_tokens']} "
        f"input tokens saved on reads\n"
        if COMPRESS
        else "compression: OFF (baseline)\n"
    )
    print(
        f"\n=== run report ===\n"
        f"mode={mode}  model={model}  steps={steps}\n"
        f"kb_search calls={kb_calls}  file reads={file_reads}\n"
        f"{compress_note}"
        f"tokens: in={in_tok}  out={out_tok}  total={in_tok + out_tok}"
    )
    await engine.dispose()
    return 0


async def _search_only(query: str) -> int:
    engine = create_async_engine(os.environ["DATABASE_URL"])
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        active = (
            await session.execute(
                text("SELECT kb_version, build_seq FROM kb_build_run WHERE status = 'active'")
            )
        ).one_or_none()
    if active is None:
        print("No active KB version.")
        return 1
    print(await kb_search(sessions, active.build_seq, query))
    await engine.dispose()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Simple KB-first coding agent (ADR-0025).")
    parser.add_argument("task", nargs="*", help="the task or question")
    parser.add_argument("--search", metavar="QUERY", help="run kb_search only (no LLM), then exit")
    parser.add_argument("--no-kb", action="store_true", help="baseline: native tools only, no KB")
    parser.add_argument(
        "--no-compress", action="store_true", help="baseline: raw file reads, no skeletons"
    )
    args = parser.parse_args()
    if args.no_compress:
        global COMPRESS
        COMPRESS = False
    if args.search:
        return asyncio.run(_search_only(args.search))
    if not args.task:
        parser.error("provide a task, or use --search QUERY")
    return asyncio.run(run_task(" ".join(args.task), use_kb=not args.no_kb))


if __name__ == "__main__":
    sys.exit(main())
