"""Task-context eval: does the knowledge graph give an agent the FULL context a real
task needs, within a token budget, instead of reading whole files?

For each golden task (a realistic dev request) we:
  1. seed-retrieve artifacts by keyword overlap with the task (what flat search finds),
  2. expand through the graph (BFS, trust-aware) to pull in connected context,
  3. pack artifacts in BFS order until a token budget, then score:
       - context_recall = gold files surfaced / gold files needed
       - tokens_used    = evidence tokens spent (<= budget)
       - read_files_tok = cost of reading those gold files whole (from disk)

Run in three modes to decompose the graph's value and decide tuning EMPIRICALLY:
  flat          = seeds only (no graph)        -> what plain retrieval gives
  deterministic = + EXTRACTED edges            -> defined_in / calls / imports backbone
  semantic      = + INFERRED edges too         -> judge documents / implements / mentions

Usage (from evals/, against the live build):
    TASK_CONTEXT_DB=agentic_kb uv run python task_context_eval.py
"""

import asyncio
import math
import os
import re
from collections import deque
from pathlib import Path

import asyncpg  # type: ignore[import-untyped]
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
DB = os.environ.get("TASK_CONTEXT_DB", "agentic_kb")
DSN = os.environ.get(
    "TASK_CONTEXT_DSN", f"postgresql://{os.environ.get('USER', '')}@localhost:5432/{DB}"
)
TOKEN_BUDGET = int(
    os.environ.get("TASK_CONTEXT_BUDGET", "7000")
)  # initial Evidence Pack (6-8k rule)
SEED_K = int(os.environ.get("TASK_CONTEXT_SEED_K", "8"))

EXTRACTED_EDGES = frozenset({"defined_in", "calls", "imports", "exposes", "tests"})
INFERRED_EDGES = frozenset({"documents", "implements", "mentions", "requests"})
# Each mode is an ordered list of edge-type TIERS. Expansion drains a whole tier (closest
# first) before the next, so a higher-trust tier always fills the budget first and a noisy
# lower tier can only ADD context in remaining space — never crowd the backbone out.
MODES: dict[str, list[frozenset[str]]] = {
    "flat": [],
    "deterministic": [EXTRACTED_EDGES],
    "semantic": [EXTRACTED_EDGES, INFERRED_EDGES],
}
_WORD = re.compile(r"[a-z_][a-z0-9_]{2,}")


def _toks(text: str) -> int:
    return max(1, len(text) // 4)


def _doc_freq(arts: list[dict], words: set[str]) -> dict[str, int]:
    """How many artifacts each word appears in (title+path+body) — for IDF weighting."""
    df = dict.fromkeys(words, 0)
    for a in arts:
        blob = f"{a['title']} {a['path'] or ''} {a['body']}".lower()
        for w in words:
            if w in blob:
                df[w] += 1
    return df


def _seed(arts: list[dict], query: str, k: int, df: dict[str, int], n: int) -> list[str]:
    """Top-k artifact ids by IDF-weighted keyword overlap. Rare identifiers (search_text,
    ledger, budget) dominate generic task words (add, new, code) that match everything."""
    qwords = set(_WORD.findall(query.lower()))
    # IDF with a floor so a term topical to THIS repo (broker, budget, context) still
    # counts — pure IDF zeroes it out in a codebase about that very topic.
    weights = {w: max(math.log(n / (1 + df.get(w, 0))), 0.5) for w in qwords}
    scored: list[tuple[float, str]] = []
    for a in arts:
        title, body, path = a["title"].lower(), a["body"].lower(), (a["path"] or "").lower()
        basename = path.rsplit("/", 1)[-1]
        # A query word inside the file's own name is the strongest, IDF-immune signal
        # (the agent's task names the file): "budget" -> budgets.py, "ledger" -> ledger.py.
        score = 5.0 * sum(w in basename for w in qwords)
        score += sum(
            weights[w]
            * ((3 if w in title else 0) + (2 if w in path else 0) + (1 if w in body else 0))
            for w in qwords
            if weights[w] > 0
        )
        if score > 0:
            scored.append((score, a["aid"]))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [aid for _, aid in scored[:k]]


def _tiered_order(
    seeds: list[str], adj: dict[str, list[tuple[str, str]]], tiers: list[frozenset[str]]
) -> list[str]:
    """Artifact ids in trust-tiered BFS order: drain each edge tier (closest first) from
    everything reached so far before moving to the next, lower-trust tier."""
    order = list(seeds)
    seen = set(seeds)
    for allowed in tiers:
        q = deque(order)  # expand this tier from ALL nodes gathered by higher tiers
        while q:
            for nbr, etype in adj.get(q.popleft(), []):
                if etype in allowed and nbr not in seen:
                    seen.add(nbr)
                    order.append(nbr)
                    q.append(nbr)
    return order


def _pack(order: list[str], by_id: dict[str, dict], budget: int) -> tuple[set[str], int]:
    """Take artifacts in order until the token budget; return their file paths + tokens."""
    paths: set[str] = set()
    used = 0
    for aid in order:
        a = by_id.get(aid)
        if a is None:
            continue
        used += _toks(a["body"] or a["title"])
        if used > budget:
            break
        if a["path"]:
            paths.add(a["path"])
    return paths, min(used, budget)


def _read_files_tokens(gold: list[str]) -> tuple[int, list[str]]:
    """Real 'read the whole file' cost from disk + which gold files are missing on disk."""
    total, missing = 0, []
    for rel in gold:
        fp = REPO_ROOT / rel
        if fp.is_file():
            total += _toks(fp.read_text(encoding="utf-8", errors="ignore"))
        else:
            missing.append(rel)
    return total, missing


async def main() -> None:
    tasks = yaml.safe_load((Path(__file__).parent / "task_context_tasks.yaml").read_text())["tasks"]
    conn = await asyncpg.connect(DSN)
    try:
        rows = await conn.fetch(
            "SELECT a.artifact_id::text aid, a.artifact_type atype, coalesce(a.title,'') title, "
            "coalesce(a.body_text,'') body, s.path FROM knowledge_artifact a "
            "JOIN source_item s ON s.source_id=a.source_id "
            "WHERE a.invalidated_at_seq IS NULL AND s.is_deleted IS FALSE"
        )
        edges = await conn.fetch(
            "SELECT from_artifact_id::text f, to_artifact_id::text t, edge_type "
            "FROM knowledge_edge WHERE invalidated_at_seq IS NULL"
        )
    finally:
        await conn.close()

    arts = [dict(r) for r in rows]
    by_id = {a["aid"]: a for a in arts}
    indexed_paths = {a["path"] for a in arts if a["path"]}
    adj: dict[str, list[tuple[str, str]]] = {}
    for e in edges:  # undirected for reachability
        adj.setdefault(e["f"], []).append((e["t"], e["edge_type"]))
        adj.setdefault(e["t"], []).append((e["f"], e["edge_type"]))

    all_qwords: set[str] = set()
    for task in tasks:
        all_qwords |= set(_WORD.findall(task["query"].lower()))
    df = _doc_freq(arts, all_qwords)

    print(
        f"KB={DB} artifacts={len(arts)} edges={len(edges)} "
        f"budget={TOKEN_BUDGET} seed_k={SEED_K}\n"
    )
    agg: dict[str, list[float]] = {m: [] for m in MODES}
    for task in tasks:
        gold = [g for g in task["gold_files"] if g in indexed_paths]
        not_indexed = [g for g in task["gold_files"] if g not in indexed_paths]
        read_tok, _missing = _read_files_tokens(task["gold_files"])
        seeds = _seed(arts, task["query"], SEED_K, df, len(arts))
        print(f"## {task['id']}  (gold={len(gold)} indexed, read-whole={read_tok} tok)")
        if not_indexed:
            print(f"   ! gold files NOT in KB: {not_indexed}")
        for mode, tiers in MODES.items():
            order = _tiered_order(seeds, adj, tiers)
            paths, used = _pack(order, by_id, TOKEN_BUDGET)
            hit = sorted(set(gold) & paths)
            recall = len(hit) / len(gold) if gold else 0.0
            agg[mode].append(recall)
            print(f"   {mode:13s} recall={recall:.2f} ({len(hit)}/{len(gold)})  tokens={used}")
        print()

    print("=== AGGREGATE (mean context_recall) ===")
    for mode in MODES:
        vals = agg[mode]
        print(f"   {mode:13s} {sum(vals) / len(vals):.3f}")


if __name__ == "__main__":
    asyncio.run(main())
