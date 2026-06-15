"""Seed an Azure DevOps project with demo wiki pages + work items (one-time test data).

Creates a handful of *relevant* wiki pages and work items in your ADO project so the
production-source build (azure_wiki / ado_card connectors) has real content to ingest.
Idempotent: wiki pages are upserted by path; work items are skipped if a same-title one
already exists. Authenticates with a PAT read from $ADO_PAT (Basic auth) — the value is
never logged.

Run from the kb-builder venv (it has httpx):
    cd services/kb-builder
    ADO_PAT=... uv run python ../../scripts/seed_ado.py
Override targets via env: ADO_ORG, ADO_PROJECT, ADO_WIKI.
"""

import base64
import os
import sys

import httpx

ORG = os.environ.get("ADO_ORG", "docodex-testing")
PROJECT = os.environ.get("ADO_PROJECT", "agentic-kb-platform")
WIKI = os.environ.get("ADO_WIKI", "agentic-kb-platform.wiki")
API = "api-version=7.1"
BASE = f"https://dev.azure.com/{ORG}/{PROJECT}/_apis"

# Demo content — relevant to THIS platform so wikify produces meaningful summaries.
WIKI_PAGES: list[tuple[str, str]] = [
    (
        "/Architecture/Context Broker",
        "# Context Broker\n\nThe MCP Context Broker is the single mediated path agents use to reach "
        "knowledge. It enforces identity, team ACLs, per-run and per-agent token budgets, "
        "deduplication, and exposes evidence by handle (evidence cards) before any raw text. Agents "
        "never touch Postgres or Azure AI Search directly. Every retrieval writes a retrieval_event "
        "to the ledger, so an operator can audit exactly what an agent did.\n",
    ),
    (
        "/Architecture/Knowledge Registry",
        "# Knowledge Registry\n\nPostgres is the source of truth. Source pointers, chunks, summaries, "
        "concepts, code symbols, graph edges, caches, build runs, and the retrieval ledger live in "
        "Postgres. Azure AI Search is a derived, rebuildable projection — never truth. A kb_version "
        "becomes active only after the publish gates (index consistency, citation integrity, "
        "evidence recall) pass.\n",
    ),
    (
        "/Architecture/Nightly Build",
        "# Nightly Incremental Build\n\nThe knowledge base is rebuilt incrementally. If a source's "
        "content hash is unchanged, the build skips wikify, graphify, embedding, and indexing for it "
        "— so cost scales with change, not corpus size. Code is extracted deterministically by "
        "graphify (no LLM); only prose sources (docs, wiki, work items) go through the LLM wikify "
        "step.\n",
    ),
    (
        "/Architecture/Evidence Cards",
        "# Evidence Cards\n\nEvidence is exposed to agents by handle, not as raw text. An evidence "
        "card (L0/L1) carries a stable id, a short citable claim, and provenance (source uri, "
        "version, span) — but not the full chunk. An agent opens raw text (L2+) only on demand via "
        "context.open_evidence, by handle. This keeps the initial Evidence Pack inside the token "
        "budget and forces every agent claim to cite a verifiable evidence id.\n",
    ),
    (
        "/Architecture/Graph Model",
        "# Graph Model\n\nThe knowledge graph is V1, not a graph database. Nodes (knowledge "
        "artifacts) and edges (knowledge_edge: edge_type, confidence, source, kb_version, "
        "trust_class) live in Postgres tables. Graph behavior — neighborhood expansion, traversal — "
        "is exposed only through MCP graph tools, so the storage backend can be swapped later "
        "without changing agents. Edges carry a trust class so traversal can prefer deterministic "
        "links over LLM-inferred ones.\n",
    ),
    (
        "/Architecture/Code Is Graphify Only",
        "# Code Is Graphify-Only (ADR-0018)\n\nSource code is never sent to the LLM. Graphify "
        "extracts code structure deterministically and a Python ast pass recovers each symbol's "
        "exact source span, which becomes the symbol's citable body_text — no tokens spent, no "
        "summary invented. The LLM is reserved for prose (docs, wiki pages, work items) and for the "
        "relationship judge. This keeps nightly build cost proportional to prose change, not "
        "codebase size.\n",
    ),
]

# (work item type, title, description). Types valid in the Basic process (Epic/Issue/Task);
# the project uses Basic, where "User Story"/"Bug" do not exist.
WORK_ITEMS: list[tuple[str, str, str]] = [
    (
        "Issue",
        "[kb-demo] Ask how retrieval works and get cited evidence",
        "As an engineer I want to ask the assistant how a feature works and receive an answer whose "
        "every claim cites a verifiable evidence id, so I can trust and follow up on it.",
    ),
    (
        "Issue",
        "[kb-demo] Nightly build skips unchanged sources",
        "As an operator I want the nightly build to skip sources whose content has not changed so we "
        "do not spend LLM or embedding tokens re-processing unchanged code and docs.",
    ),
    (
        "Issue",
        "[kb-demo] create_pack failed with Decimal*float against real Postgres",
        "The keyword search returned Decimal scores from SQL NUMERIC arithmetic and the ranker "
        "multiplied them by a float temporal weight, raising 'unsupported operand type(s) for *'. "
        "Fixed by coercing scores to float at the database boundary.",
    ),
    (
        "Task",
        "[kb-demo] Wire the scheduled nightly CI pipeline (ADR-0004)",
        "The build plane and publish gates are implemented and exercised in PR CI, but no cron "
        "trigger runs the nightly build against a real source set yet. Add a scheduled workflow "
        "once an Azure target is provisioned.",
    ),
    (
        "Task",
        "[kb-demo] Add deterministic code search_text (PR-34)",
        "Phase 2 of ADR-0018: derive a separate search_text from the AST (docstrings, split "
        "identifiers, signatures, called names) so concept queries reach code symbols, while "
        "body_text stays the exact citable span. Still zero LLM for code.",
    ),
    (
        "Issue",
        "[kb-demo] Visualize the knowledge graph as an Obsidian vault",
        "As a developer I want to export the active kb_version as an Obsidian vault of linked notes "
        "so I can browse artifacts and their graph edges visually and sanity-check what the build "
        "produced before pointing agents at it.",
    ),
    (
        "Task",
        "[kb-demo] Widen graphify scope to the full service source",
        "Expand the production test source set from a representative subset to both services' full "
        "src trees to exercise graphify at scale and confirm cross-file import/call edges resolve "
        "without unresolved-key drops.",
    ),
]


def _client(pat: str) -> httpx.Client:
    token = base64.b64encode(f":{pat}".encode()).decode()
    return httpx.Client(headers={"Authorization": f"Basic {token}"}, timeout=30.0)


def _upsert_wiki_page(client: httpx.Client, path: str, content: str) -> None:
    url = f"{BASE}/wiki/wikis/{WIKI}/pages"
    params = {"path": path, "api-version": "7.1"}
    existing = client.get(url, params=params)
    headers = {"Content-Type": "application/json"}
    if existing.status_code == 200:
        headers["If-Match"] = existing.headers.get("ETag", "")
        verb = "updated"
    elif existing.status_code == 404:
        verb = "created"
    else:
        print(f"  ! wiki GET {path} -> {existing.status_code}: {existing.text[:200]}")
        return
    resp = client.put(url, params=params, headers=headers, json={"content": content})
    if resp.status_code in (200, 201):
        print(f"  wiki {verb}: {path}")
    else:
        print(f"  ! wiki PUT {path} -> {resp.status_code}: {resp.text[:200]}")


def _title_exists(client: httpx.Client, title: str) -> bool:
    wiql = {
        "query": (
            "Select [System.Id] From WorkItems Where [System.TeamProject] = @project "
            f"And [System.Title] = '{title.replace(chr(39), chr(39) * 2)}'"
        )
    }
    resp = client.post(f"{BASE}/wit/wiql?{API}", json=wiql)
    return resp.status_code == 200 and bool(resp.json().get("workItems"))


def _create_work_item(client: httpx.Client, wi_type: str, title: str, description: str) -> None:
    if _title_exists(client, title):
        print(f"  work item exists, skipping: {title}")
        return
    body = [
        {"op": "add", "path": "/fields/System.Title", "value": title},
        {"op": "add", "path": "/fields/System.Description", "value": description},
    ]
    url = f"{BASE}/wit/workitems/${wi_type}?{API}"
    resp = client.post(
        url, headers={"Content-Type": "application/json-patch+json"}, json=body
    )
    if resp.status_code in (200, 201):
        print(f"  work item created: [{wi_type}] {title}  (id {resp.json().get('id')})")
    else:
        print(f"  ! work item POST '{title}' -> {resp.status_code}: {resp.text[:200]}")


def main() -> int:
    pat = os.environ.get("ADO_PAT")
    if not pat:
        print("ADO_PAT is not set (Azure DevOps PAT with Wiki: Read&Write, Work Items: Read&Write).")
        return 1
    print(f"Seeding ADO {ORG}/{PROJECT} (wiki={WIKI}) …")
    with _client(pat) as client:
        print("Wiki pages:")
        for path, content in WIKI_PAGES:
            _upsert_wiki_page(client, path, content)
        print("Work items:")
        for wi_type, title, description in WORK_ITEMS:
            _create_work_item(client, wi_type, title, description)
    print("Done. (Re-running is safe: pages upsert, work items skip by title.)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
