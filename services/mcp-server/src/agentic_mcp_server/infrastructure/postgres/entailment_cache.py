"""entailment_cache access: the L3 verifier's gate over the LLM.

kb-builder OWNS this table (migration 0015); mcp-server NEVER runs migrations and
reaches it only via raw SQL with pinned names — the SAME boundary pattern as
retrieval_event. docs/contracts/verification-receipt.md documents the L3 cache key
and the contract tests keep the column set honest.

A cache HIT returns the stored verdict and MUST prevent the LLM call (architecture
invariant 4), exactly like generation_cache / embedding_cache. Writes are idempotent
(on-conflict-do-nothing on the composite key) so re-verifying a claim never
duplicates or overwrites a verdict.
"""

import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

ENTAILMENT_CACHE_TABLE = "entailment_cache"

# (claim_hash, evidence_ids_hash, prompt_version, model_version) is the composite
# cache key. A hit returns the stored verdict with ZERO LLM calls.
_LOOKUP_QUERY = text(
    f"""
    SELECT entailed, reason
    FROM {ENTAILMENT_CACHE_TABLE}
    WHERE claim_hash = :claim_hash
      AND evidence_ids_hash = :evidence_ids_hash
      AND prompt_version = :prompt_version
      AND model_version = :model_version
    """
)

# Idempotent insert: on-conflict-do-nothing on the composite PK so a re-verify is a
# no-op (never a duplicate row, never an overwrite of a prior verdict).
_RECORD_QUERY = text(
    f"""
    INSERT INTO {ENTAILMENT_CACHE_TABLE} (
        claim_hash, evidence_ids_hash, prompt_version, model_version, entailed, reason
    ) VALUES (
        :claim_hash, :evidence_ids_hash, :prompt_version, :model_version, :entailed, :reason
    )
    ON CONFLICT (claim_hash, evidence_ids_hash, prompt_version, model_version) DO NOTHING
    """
)


async def lookup_entailment(
    session: AsyncSession,
    *,
    claim_hash: str,
    evidence_ids_hash: str,
    prompt_version: str,
    model_version: str,
) -> tuple[bool, str] | None:
    """Return ``(entailed, reason)`` on a cache hit, else ``None`` (miss ⇒ LLM)."""
    result = await session.execute(
        _LOOKUP_QUERY,
        {
            "claim_hash": claim_hash,
            "evidence_ids_hash": evidence_ids_hash,
            "prompt_version": prompt_version,
            "model_version": model_version,
        },
    )
    row = result.first()
    hit = row is not None
    logger.info(
        "event=entailment_cache_lookup claim_hash=%s evidence_ids_hash=%s "
        "prompt_version=%s model_version=%s hit=%s",
        claim_hash,
        evidence_ids_hash,
        prompt_version,
        model_version,
        hit,
    )
    if row is None:
        return None
    return bool(row.entailed), row.reason


async def record_entailment(
    session: AsyncSession,
    *,
    claim_hash: str,
    evidence_ids_hash: str,
    prompt_version: str,
    model_version: str,
    entailed: bool,
    reason: str,
) -> None:
    """Idempotently store the verdict so the next verify of this claim skips the LLM."""
    await session.execute(
        _RECORD_QUERY,
        {
            "claim_hash": claim_hash,
            "evidence_ids_hash": evidence_ids_hash,
            "prompt_version": prompt_version,
            "model_version": model_version,
            "entailed": entailed,
            "reason": reason,
        },
    )
    await session.commit()
    logger.info(
        "event=entailment_cache_record claim_hash=%s evidence_ids_hash=%s entailed=%s",
        claim_hash,
        evidence_ids_hash,
        entailed,
    )
