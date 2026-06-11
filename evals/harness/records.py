"""Plain run records: the DB-free seam between execution and metric computation.

The executor normalizes retrieval_event ledger rows into these dataclasses;
metric unit tests construct them directly without a database.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class LedgerEvent:
    tool_name: str
    status: str  # reused | approved | denied | needs_human_approval | error
    agent_name: str
    cache_hit: bool
    semantic_reuse: bool
    tokens_returned: int
    reused_evidence_ids: tuple[str, ...]
    new_evidence_ids: tuple[str, ...]

    @property
    def evidence_ids(self) -> tuple[str, ...]:
        return (*self.reused_evidence_ids, *self.new_evidence_ids)


@dataclass(frozen=True)
class RunRecord:
    case_id: str
    task_type: str
    succeeded: bool
    expected_items: int
    missing_items: tuple[str, ...]
    total_claims: int
    unsupported_claims: int
    events: tuple[LedgerEvent, ...]

    @property
    def tokens_charged(self) -> int:
        return sum(event.tokens_returned for event in self.events)
