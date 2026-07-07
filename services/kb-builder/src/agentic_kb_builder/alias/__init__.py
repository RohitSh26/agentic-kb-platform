"""Alias/Reference index (PR-38, docs/contracts/alias-reference.md).

Deterministic, zero-LLM build-time mining of informal alias phrases (commit
subjects, PR-brief/ADR filename slugs) into `alias_reference` knowledge
artifacts + `aliases` edges, plus the pure resolver the eval harness scores.
"""

from agentic_kb_builder.alias.ledger_mining import LedgerMiningResult, run_ledger_alias_miner
from agentic_kb_builder.alias.mining import (
    AliasAggregate,
    MinedPhrase,
    SourceContribution,
    aggregate_contributions,
    mine_commit,
    mine_doc_source,
    normalize_phrase,
    phrase_variants,
)
from agentic_kb_builder.alias.resolve import AliasEntry, Resolution, resolve
from agentic_kb_builder.alias.run import (
    LEDGER_MINED_PROVENANCE,
    AliasMinerResult,
    load_alias_entries,
    run_alias_miner,
)

__all__ = [
    "LEDGER_MINED_PROVENANCE",
    "AliasAggregate",
    "AliasEntry",
    "AliasMinerResult",
    "LedgerMiningResult",
    "MinedPhrase",
    "Resolution",
    "SourceContribution",
    "aggregate_contributions",
    "load_alias_entries",
    "mine_commit",
    "mine_doc_source",
    "normalize_phrase",
    "phrase_variants",
    "resolve",
    "run_alias_miner",
    "run_ledger_alias_miner",
]
