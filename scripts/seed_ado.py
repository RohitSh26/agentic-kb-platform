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
        "/Runbooks/Nightly Build",
        "# Nightly Incremental Build\n\nThe knowledge base is rebuilt incrementally. If a source's "
        "content hash is unchanged, the build skips wikify, graphify, embedding, and indexing for it "
        "— so cost scales with change, not corpus size. Code is extracted deterministically by "
        "graphify (no LLM); only prose sources (docs, wiki, work items) go through the LLM wikify "
        "step.\n",
    ),
]

# (work item type, title, description)
WORK_ITEMS: list[tuple[str, str, str]] = [
    (
        "User Story",
        "[kb-demo] Ask how retrieval works and get cited evidence",
        "As an engineer I want to ask the assistant how a feature works and receive an answer whose "
        "every claim cites a verifiable evidence id, so I can trust and follow up on it.",
    ),
    (
        "User Story",
        "[kb-demo] Nightly build skips unchanged sources",
        "As an operator I want the nightly build to skip sources whose content has not changed so we "
        "do not spend LLM or embedding tokens re-processing unchanged code and docs.",
    ),
    (
        "Bug",
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
