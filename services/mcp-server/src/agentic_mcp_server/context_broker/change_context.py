"""context.create_change_pack: the BUILD-lane context selector.

A deterministic, ranked, EXPLAINABLE resolver (not a keyword blob). Stages:
  1. parse task hints (class names, file names)
  2. resolve TARGET files (exact symbol match > exact file match > lexical)
  3. resolve TEST files (KB test artifacts referencing the target; naming convention)
  4. resolve DEPENDENCY files (imports/calls from the target, capped)
  5. estimate tokens per file and rank
Priority is target > test > dependency: the test file teaches the repo's testing style and
matters more for writing code than a dependency. Returns a small file list with per-file
reason + numeric confidence + est_tokens; the runtime opens ONLY these files.

This tool returns a curated FILE LIST, not bytes (Postgres stays pointer-first); the runtime
reads the files from its workspace. A retrieval_event is written for the selection.
"""

import logging
import re
import time
import uuid

from agentic_mcp_server.auth.rbac import Requester
from agentic_mcp_server.context_broker.dependencies import BrokerDeps
from agentic_mcp_server.context_broker.retrieval import _readable_path
from agentic_mcp_server.domain.token_budget import estimate_tokens
from agentic_mcp_server.infrastructure.postgres.active_kb_version import fetch_active_version
from agentic_mcp_server.infrastructure.postgres.artifacts import ArtifactRow, fetch_artifacts
from agentic_mcp_server.infrastructure.postgres.edges import fetch_edges_touching
from agentic_mcp_server.infrastructure.postgres.retrieval_events import (
    RetrievalEventInsert,
    insert_event,
)
from agentic_mcp_server.mcp.tool_schemas.change import (
    ChangeContextRequest,
    ChangeContextResponse,
    FileRef,
)

logger = logging.getLogger(__name__)

_TOOL_NAME = "context.create_change_pack"
_MAX_DEPENDENCY_FILES = 2  # full dependency files (cap so we don't lose the token win)
_SEARCH_TOP = 12
_CODE_TYPES = ("code_symbol", "code_file", "endpoint", "test")
_CAPWORDS = re.compile(r"\b([A-Z][A-Za-z0-9]+(?:[A-Z][A-Za-z0-9]*)*)\b")  # ClassNames
_PYFILE = re.compile(r"\b([A-Za-z0-9_./-]+\.py)\b")


def _is_test_path(path: str) -> bool:
    base = path.rsplit("/", 1)[-1]
    return "/test" in path or base.startswith("test_") or base.endswith("_test.py")


def _file_of(artifact: ArtifactRow) -> str:
    """Repo-relative file path an artifact belongs to (its source file)."""
    return _readable_path(artifact.source_uri)


def _est_tokens_for_file(path: str, by_file_chars: dict[str, int]) -> int:
    chars = by_file_chars.get(path, 0)
    # Fall back to a small constant when we have no spans for the file (pointer-only).
    return estimate_tokens("x" * chars) if chars else 400


def _conventional_test_paths(target_path: str) -> list[str]:
    """Best-effort conventional test paths for a source file (the runtime verifies which
    exist). Covers the common `src/<pkg>/a/b.py -> tests/.../test_b.py` layouts."""
    parts = target_path.split("/")
    base = parts[-1]
    stem = base[:-3] if base.endswith(".py") else base
    test_base = f"test_{stem}.py"
    out: list[str] = []
    if "src" in parts:
        root = "/".join(parts[: parts.index("src")])  # e.g. services/kb-builder
        out.append(f"{root}/tests/unit/{test_base}")
        out.append(f"{root}/tests/{test_base}")
        # mirror the sub-package under tests/unit
        after_src = parts[parts.index("src") + 1 :]
        if len(after_src) > 2:
            sub = "/".join(after_src[1:-1])  # drop the top package + the filename
            out.append(f"{root}/tests/unit/{sub}/{test_base}")
    out.append("/".join(parts[:-1]) + f"/{test_base}")
    # de-dupe preserving order
    seen: set[str] = set()
    return [p for p in out if not (p in seen or seen.add(p))]


async def create_change_pack(
    deps: BrokerDeps, request: ChangeContextRequest, requester: Requester
) -> ChangeContextResponse:
    started = time.monotonic()
    notes: list[str] = []
    query = f"{request.task} {request.target_hint or ''}".strip()

    async with deps.session_factory() as session:
        active = await fetch_active_version(session)
    if active is None:
        return ChangeContextResponse(
            target_files=[],
            test_files=[],
            dependency_files=[],
            relevant_symbols=[],
            notes=["no active kb_version; the knowledge base has not been built yet"],
        )
    build_seq = active.build_seq

    # --- search + hydrate + ACL ------------------------------------------------------------
    hits = await deps.search_client.search(query, build_seq=build_seq, top=_SEARCH_TOP)
    scores = {h.artifact_id: h.score for h in hits}
    async with deps.session_factory() as session:
        rows = await fetch_artifacts(session, list(scores), build_seq)
    allowed = deps.authorization.filter_artifacts(requester, rows)
    code = [a for a in allowed if a.artifact_type in _CODE_TYPES]
    code.sort(key=lambda a: scores.get(a.artifact_id, 0.0), reverse=True)

    # token estimate per file from the spans we know
    by_file_chars: dict[str, int] = {}
    for a in allowed:
        by_file_chars[_file_of(a)] = by_file_chars.get(_file_of(a), 0) + len(a.body_text or "")

    # --- stage 1: parse hints --------------------------------------------------------------
    hint_text = f"{request.task} {request.target_hint or ''}"
    class_hints = {m.group(1) for m in _CAPWORDS.finditer(hint_text)}
    file_hints = {m.group(1).rsplit("/", 1)[-1] for m in _PYFILE.finditer(hint_text)}

    # --- stage 2: target files -------------------------------------------------------------
    target_files: list[FileRef] = []
    target_symbol_ids: list[uuid.UUID] = []
    target_paths: set[str] = set()
    relevant_symbols: list[str] = []

    def _add_target(a: ArtifactRow, reason: str, conf: float) -> None:
        path = _file_of(a)
        if a.artifact_type == "code_symbol":
            target_symbol_ids.append(a.artifact_id)
            if a.title:
                relevant_symbols.append(a.title)
        if path in target_paths or _is_test_path(path):
            return
        target_paths.add(path)
        target_files.append(
            FileRef(
                path=path,
                reason=reason,
                confidence=conf,
                est_tokens=_est_tokens_for_file(path, by_file_chars),
            )
        )

    for a in code:
        title = (a.title or "").rstrip("()")
        if title and title in class_hints:
            _add_target(a, f"contains exact symbol {title} matched from the task", 0.96)
    if not target_files:
        for a in code:
            base = _file_of(a).rsplit("/", 1)[-1]
            if base in file_hints:
                _add_target(a, f"file name {base} named in the task", 0.85)
    if not target_files and code:
        _add_target(code[0], "top lexical match for the task", 0.55)
        notes.append("target resolved by lexical fallback (no exact symbol/file hint matched)")

    # --- graph neighbours of the targets ---------------------------------------------------
    seed_ids = list(
        {a.artifact_id for a in code if _file_of(a) in target_paths} | set(target_symbol_ids)
    )
    seed_set = set(seed_ids)
    edges = []
    neighbour_ids: set[uuid.UUID] = set()
    if seed_ids:
        async with deps.session_factory() as session:
            edges = await fetch_edges_touching(session, seed_ids, build_seq, None)
        for e in edges:
            other = e.to_artifact_id if e.from_artifact_id in seed_set else e.from_artifact_id
            neighbour_ids.add(other)
        async with deps.session_factory() as session:
            neighbour_rows = await fetch_artifacts(session, list(neighbour_ids), build_seq)
        neighbours = {
            a.artifact_id: a for a in deps.authorization.filter_artifacts(requester, neighbour_rows)
        }
        for a in neighbours.values():
            by_file_chars[_file_of(a)] = by_file_chars.get(_file_of(a), 0) + len(a.body_text or "")
    else:
        neighbours = {}

    # --- stage 3: test files (graph tests-edges; KB test artifacts; naming convention) -----
    test_files: list[FileRef] = []
    test_paths: set[str] = set()

    def _add_test(path: str, reason: str, conf: float) -> None:
        if path in test_paths:
            return
        test_paths.add(path)
        test_files.append(
            FileRef(
                path=path,
                reason=reason,
                confidence=conf,
                est_tokens=_est_tokens_for_file(path, by_file_chars),
            )
        )

    target_symbol_set = set(target_symbol_ids)
    for e in edges:
        if e.edge_type == "tests" and e.to_artifact_id in target_symbol_set:
            t = neighbours.get(e.from_artifact_id)
            if t:
                _add_test(_file_of(t), f"`tests` edge → a target symbol ({t.title})", 0.9)
    for a in allowed:  # KB test artifacts referencing the target symbol/module
        p = _file_of(a)
        if _is_test_path(p) and (
            a.title in relevant_symbols
            or any(h.lower() in (a.body_text or "").lower() for h in class_hints)
        ):
            _add_test(p, "test file in the KB references the target symbol", 0.8)
    if not test_files:
        # No `tests` edge and no test among the task's own hits — run a FOCUSED search for the
        # target's test file. This finds a real test like ``test_github_rest_backend.py`` whenever
        # tests are in the KB, without guessing the filename (works regardless of `tests` edges).
        stems = [p.rsplit("/", 1)[-1].removesuffix(".py") for p in target_paths]
        test_query = "test " + " ".join(list(relevant_symbols)[:2] + stems)
        test_hits = await deps.search_client.search(test_query, build_seq=build_seq, top=8)
        async with deps.session_factory() as session:
            test_rows = await fetch_artifacts(
                session, [h.artifact_id for h in test_hits], build_seq
            )
        for a in deps.authorization.filter_artifacts(requester, test_rows):
            p = _file_of(a)
            if _is_test_path(p):
                by_file_chars[p] = by_file_chars.get(p, 0) + len(a.body_text or "")
                _add_test(p, "KB search located the target's test file", 0.7)
    if not test_files:
        for tp in target_paths:
            for cand in _conventional_test_paths(tp):
                _add_test(cand, "naming convention (runtime verifies existence)", 0.5)
        if test_files:
            notes.append(
                "test file proposed by naming convention — the runtime must verify it exists"
            )

    # --- stage 4: dependency files (imports/calls, capped) ---------------------------------
    dependency_files: list[FileRef] = []
    dep_paths: set[str] = set()
    for e in edges:
        if e.edge_type not in ("imports", "calls"):
            continue
        other = e.to_artifact_id if e.from_artifact_id in seed_set else e.from_artifact_id
        a = neighbours.get(other)
        if a is None:
            continue
        path = _file_of(a)
        if path in target_paths or path in dep_paths or _is_test_path(path):
            continue
        if len(dependency_files) >= _MAX_DEPENDENCY_FILES:
            break
        dep_paths.add(path)
        kind = "imported by" if e.edge_type == "imports" else "called from"
        dependency_files.append(
            FileRef(
                path=path,
                reason=f"{kind} a target file ({e.edge_type})",
                confidence=0.6,
                est_tokens=_est_tokens_for_file(path, by_file_chars),
            )
        )
        if a.artifact_type == "code_symbol" and a.title:
            relevant_symbols.append(a.title)

    # --- budget enforcement (invariant 3: the BROKER caps tokens, not the caller) ----------
    # Target + test are essential and always kept; dependency files are trimmed (lowest
    # priority first) so the selected est_tokens stays within budget_tokens.
    essential = sum(f.est_tokens for f in target_files + test_files)
    spent = essential
    kept_deps: list[FileRef] = []
    for f in dependency_files:
        if spent + f.est_tokens > request.budget_tokens:
            continue
        kept_deps.append(f)
        spent += f.est_tokens
    if len(kept_deps) < len(dependency_files):
        notes.append(
            f"dropped {len(dependency_files) - len(kept_deps)} dependency file(s) to stay "
            f"within budget_tokens={request.budget_tokens}"
        )
    dependency_files = kept_deps

    # --- ledger ----------------------------------------------------------------------------
    selected = [f.path for f in target_files + test_files + dependency_files]
    async with deps.session_factory() as session:
        await insert_event(
            session,
            RetrievalEventInsert(
                run_id=request.run_id or "-",
                agent_name=requester.subject,
                tool_name=_TOOL_NAME,
                status="approved",
                kb_version=active.kb_version,
                query_text=query,
                latency_ms=int((time.monotonic() - started) * 1000),
                details={
                    "task": request.task,
                    "target_files": [f.path for f in target_files],
                    "test_files": [f.path for f in test_files],
                    "dependency_files": [f.path for f in dependency_files],
                    "notes": notes,
                },
            ),
        )

    logger.info(
        "broker.create_change_pack subject=%s targets=%d tests=%d deps=%d selected=%s",
        requester.subject,
        len(target_files),
        len(test_files),
        len(dependency_files),
        selected,
    )
    return ChangeContextResponse(
        target_files=target_files,
        test_files=test_files,
        dependency_files=dependency_files,
        relevant_symbols=list(dict.fromkeys(relevant_symbols)),
        notes=notes,
    )
