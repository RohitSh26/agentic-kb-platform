"""Run-scoped Evidence Pack state.

Held in process memory in V1 (single-instance deployment; recorded in
docs/contracts/mcp-tools-contract.md): packs are a runtime cache, never truth —
the durable, auditable record is the retrieval_event ledger in Postgres.
"""

import asyncio
import uuid
from dataclasses import dataclass, field

from agentic_mcp_server.context_broker.budgets import AgentUsage
from agentic_mcp_server.context_broker.dedupe import QueryHistory
from agentic_mcp_server.mcp.tool_schemas.evidence import EvidenceCard


class UnknownPackError(KeyError):
    pass


@dataclass
class EvidencePackState:
    context_pack_id: str
    run_id: str
    kb_version: str
    # The active build's build_seq when the pack was created: every later
    # read/expand/request_more on this pack serves the SAME membership snapshot
    # (version-membership.md), pinning the pack to one consistent version.
    build_seq: int
    retrieval_profile: str
    summary: str
    budget_tokens: int
    used_run_tokens: int = 0
    cards: dict[str, EvidenceCard] = field(default_factory=dict)
    open_questions: list[str] = field(default_factory=list)
    agent_usage: dict[str, AgentUsage] = field(default_factory=dict)
    history: QueryHistory = field(default_factory=QueryHistory)
    # serializes check-then-charge so concurrent calls cannot both pass a
    # budget check before either charges (budgets are enforced, not advisory)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def run_remaining_tokens(self) -> int:
        return max(self.budget_tokens - self.used_run_tokens, 0)

    def usage_for(self, subject: str) -> AgentUsage:
        return self.agent_usage.setdefault(subject, AgentUsage())

    def charge(self, subject: str, tokens: int) -> None:
        self.used_run_tokens += tokens
        self.usage_for(subject).tokens += tokens


@dataclass
class PackStore:
    packs: dict[str, EvidencePackState] = field(default_factory=dict)
    # Per-RUN, per-subject usage shared across every pack of a run. Each new pack
    # for a run reuses these counters, so an agent cannot reset its per-agent
    # allowance by re-creating the pack within the run (the create_pack ceiling
    # bypass): the follow-up meter request_more reads is run-scoped, not
    # pack-scoped. Outlives any single pack so the ceiling holds across re-packs.
    run_usage: dict[str, dict[str, AgentUsage]] = field(default_factory=dict)
    # bounds process memory in a long-lived instance; evicted packs remain
    # auditable through the ledger
    max_packs: int = 256

    def usage_for_run(self, run_id: str) -> dict[str, AgentUsage]:
        return self.run_usage.setdefault(run_id, {})

    def create(self, pack: EvidencePackState) -> None:
        while len(self.packs) >= self.max_packs:
            self.packs.pop(next(iter(self.packs)))
        self.packs[pack.context_pack_id] = pack

    def get(self, context_pack_id: str) -> EvidencePackState:
        try:
            return self.packs[context_pack_id]
        except KeyError as exc:
            raise UnknownPackError(context_pack_id) from exc


def new_pack_id() -> str:
    return str(uuid.uuid4())
