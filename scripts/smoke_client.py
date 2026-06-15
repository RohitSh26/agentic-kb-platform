"""Smoke-call the running MCP broker through its public tool surface (dev-guide 06).

Drives the same five tools a real agent would, in order, against a locally-running
server (loopback, dev-auth). Prints what each call proves. Exits non-zero if the
broker is unreachable or create_pack returns no evidence (a broken KB/build).

Usage (the e2e script sets these):
    MCP_URL=http://127.0.0.1:8765/mcp/ uv run python scripts/smoke_client.py
"""

import asyncio
import os
import sys

from fastmcp import Client

URL = os.environ.get("MCP_URL", "http://127.0.0.1:8765/mcp/")
# Under dev-auth (ADR-0016) the verifier ignores the token value on a loopback
# bind and mints a fixed dev identity, so ANY non-empty bearer works here.
BEARER = os.environ.get("MCP_BEARER", "local-dev-token")
TASK = os.environ.get(
    "MCP_DEMO_TASK", "What recent changes were made to the verifier and the build index?"
)


def _ok(label: str, detail: str) -> None:
    print(f"  \033[32mok\033[0m  {label}: {detail}")


def _d(result: object) -> dict:
    """The tool's structured payload as a plain dict (fastmcp returns a model in
    ``.data`); unwrap a single ``result`` envelope if the transport added one."""
    sc = getattr(result, "structured_content", None)
    if sc is None:
        sc = getattr(result, "data", None)
        sc = sc.model_dump() if hasattr(sc, "model_dump") else sc
    if isinstance(sc, dict) and list(sc.keys()) == ["result"]:
        return sc["result"]
    assert isinstance(sc, dict), f"unexpected tool result shape: {type(sc)}"
    return sc


async def main() -> int:
    print(f"connecting to {URL} (dev-auth bearer)\n")
    async with Client(URL, auth=BEARER) as client:
        # 1) create_pack — retrieve ONCE, return cards by handle (not raw text),
        #    within budget; identity/ACL/budget come from the session, not the body.
        pack = _d(
            await client.call_tool(
                "context.create_pack",
                {
                    "request": {
                        "run_id": "demo-run-1",
                        "task": TASK,
                        "approved_context_plan": "recent commit history relevant to the task",
                        "retrieval_profile": "default",
                        "budget_tokens": 6000,
                        "intent": "how_does_x_work",
                    }
                },
            )
        )
        pack_id = pack["context_pack_id"]
        cards = pack["evidence_cards"]
        _ok("create_pack", f"kb_version={pack['kb_version']} cards={len(cards)} pack={pack_id}")
        if not cards:
            print(
                "\nno evidence cards returned — the active KB is empty or the task matched "
                "nothing. Re-run the build step.",
                file=sys.stderr,
            )
            return 1

        # 2) open_evidence — raw text reachable ONLY by handle, metered against the
        #    pack budget, and flagged (never rewritten) by the injection scan.
        first = cards[0]["evidence_id"]
        opened = _d(
            await client.call_tool(
                "context.open_evidence",
                {"request": {"context_pack_id": pack_id, "evidence_id": first, "max_tokens": 1500}},
            )
        )
        _ok("open_evidence", f"level={opened['level']} injection_flagged={opened['injection_flagged']}")

        # 3) graph.get_neighbors — graph behaviour ONLY through this tool over the
        #    Postgres knowledge_edge table (EXTRACTED edges by default).
        neighbors = _d(
            await client.call_tool(
                "graph.get_neighbors",
                {
                    "request": {
                        "artifact_id": cards[0]["artifact_id"],
                        "depth": 1,
                        "trust_floor": "EXTRACTED",
                    }
                },
            )
        )
        edge_types = [n["edge_type"] for n in neighbors["neighbors"]]
        _ok("get_neighbors", f"{len(edge_types)} neighbor(s) {edge_types}")

        # 4) verify_answer — the trust boundary: every claim cites evidence ids; L0
        #    runs the mandatory deterministic provenance checks and returns a receipt.
        receipt = _d(
            await client.call_tool(
                "context.verify_answer",
                {
                    "request": {
                        "answer_id": "demo-answer-1",
                        "claims": [
                            {
                                "claim_id": "c1",
                                "text": "This change is recorded in the cited commit.",
                                "evidence_ids": [first],
                            }
                        ],
                        "verifier_levels": ["L0"],
                    }
                },
            )
        )
        _ok("verify_answer", f"overall={receipt['overall']}")

        # 5) list_retrievals — every retrieval path is ledgered; inspect what the
        #    broker did on your behalf for this run.
        ledger = _d(
            await client.call_tool("ledger.list_retrievals", {"request": {"run_id": "demo-run-1"}})
        )
        _ok("list_retrievals", f"{len(ledger['events'])} ledgered event(s) for demo-run-1")

    print("\nsmoke passed — the full broker path works locally end to end.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
