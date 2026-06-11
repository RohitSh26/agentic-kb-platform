"""Run-scoped Evidence Pack state.

Held in process memory in V1 (single-instance deployment; recorded in
docs/contracts/mcp-tools-contract.md): packs are a runtime cache, never truth —
the durable, auditable record is the retrieval_event ledger in Postgres.
"""

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
    retrieval_profile: str
    summary: str
    budget_tokens: int
    used_run_tokens: int = 0
    cards: dict[str, EvidenceCard] = field(default_factory=dict)
    open_questions: list[str] = field(default_factory=list)
    agent_usage: dict[str, AgentUsage] = field(default_factory=dict)
    history: QueryHistory = field(default_factory=QueryHistory)

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

    def create(self, pack: EvidencePackState) -> None:
        self.packs[pack.context_pack_id] = pack

    def get(self, context_pack_id: str) -> EvidencePackState:
        try:
            return self.packs[context_pack_id]
        except KeyError as exc:
            raise UnknownPackError(context_pack_id) from exc


def new_pack_id() -> str:
    return str(uuid.uuid4())
