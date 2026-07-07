"""Smoke-call the running MCP broker through its public tool surface (dev-guide 04-review-drafts).

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
                "context_create_pack",
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
        print(f'  the agent asked: "{TASK}"\n')
        _ok("create_pack", f"kb_version={pack['kb_version']} retrieved {len(cards)} evidence card(s)")
        if not cards:
            print(
                "\nno evidence cards returned — the active KB is empty or the task matched "
                "nothing. Re-run the build step.",
                file=sys.stderr,
            )
            return 1
        for c in cards:
            print(f"        · [{c['card_type']}] {c['title']}  ({c['tokens_if_expanded']} tok if opened)")

        # 2) open_evidence — raw text reachable ONLY by handle, metered against the
        #    pack budget, and flagged (never rewritten) by the injection scan.
        first = cards[0]["evidence_id"]
        opened = _d(
            await client.call_tool(
                "context_open_evidence",
                {"request": {"context_pack_id": pack_id, "evidence_id": first, "max_tokens": 1500}},
            )
        )
        snippet = " ".join(opened.get("untrusted_content", "").split())[:160]
        _ok("open_evidence", f"opened card 1 as {opened['level']} text (injection_flagged={opened['injection_flagged']})")
        print(f'        untrusted_content: "{snippet}…"')

        # 3) graph_get_neighbors — graph behaviour ONLY through this tool over the
        #    Postgres knowledge_edge table (EXTRACTED edges by default).
        neighbors = _d(
            await client.call_tool(
                "graph_get_neighbors",
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
        _ok("get_neighbors", f"walked the graph from card 1 → {len(edge_types)} EXTRACTED neighbor(s) {edge_types}")

        # 3.5) context_expand — the keystone: from the retrieved cards, walk the graph
        #      trust-tiered (EXTRACTED backbone first) to pull the FULL connected code
        #      context in ONE governed call, charged against the pack budget — so the
        #      agent reaches a symbol's file, callees and imports without reading files.
        seeds = [c["artifact_id"] for c in cards[:3]]
        expanded = _d(
            await client.call_tool(
                "context_expand",
                {
                    "request": {
                        "context_pack_id": pack_id,
                        "seed_artifact_ids": seeds,
                        "trust_floor": "EXTRACTED",
                        "include_inferred": False,
                        "budget_tokens": 4000,
                    }
                },
            )
        )
        exp = expanded["cards"]
        _ok(
            "context_expand",
            f"expanded {len(seeds)} seed card(s) → {len(exp)} connected card(s) "
            f"({expanded['tokens_used']} tok, truncated={expanded['truncated']})",
        )
        for c in exp[:6]:
            print(f"        · [{c['card_type']}] {c['title']}")

        # 4) verify_answer — the trust boundary: every claim cites evidence ids; L0
        #    runs the mandatory deterministic provenance checks and returns a receipt.
        receipt = _d(
            await client.call_tool(
                "context_verify_answer",
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
        checks = receipt["claim_results"][0]["checks"]
        passed = [name.replace("L0_", "") for name, ok in checks.items() if ok is True]
        _ok("verify_answer", f"claim citing card 1 → overall={receipt['overall']}")
        print(f"        L0 provenance checks passed: {', '.join(passed)}")

        # 5) list_retrievals — every retrieval path is ledgered; inspect what the
        #    broker did on your behalf for this run.
        ledger = _d(
            await client.call_tool("ledger_list_retrievals", {"request": {"run_id": "demo-run-1"}})
        )
        events = ledger["events"]
        _ok("list_retrievals", f"{len(events)} ledgered event(s) for run 'demo-run-1':")
        for e in events:
            print(f"        · {e.get('tool')} → {e.get('status')}  ({e.get('tokens_returned', 0)} tok)")

    print(
        "\nsmoke passed — what just happened, in one sentence:\n"
        "  an agent (identity 'local-dev') asked a question; the broker retrieved evidence ONCE\n"
        "  and returned cards by handle within budget, expanded one card's raw text on demand,\n"
        "  walked the Postgres graph, and issued a verification RECEIPT for a claim that cited its\n"
        "  evidence — and every step was written to the retrieval ledger (the audit trail above).\n"
        "  See dev-guide 04-review-drafts §'What just happened?' to inspect the ledger + receipts in Postgres."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
